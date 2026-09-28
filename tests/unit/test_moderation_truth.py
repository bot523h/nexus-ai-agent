"""Moderation truth: the output gate must fail closed, not open.

``PhiAgent.moderate`` is the last filter between generated text and the user.
It used to answer ``{"safe": True, "reason": "parse_error"}`` whenever the model
replied with anything that was not JSON — reporting "I could not evaluate this"
in the field that means "I evaluated it and it is fine". Every other gate in this
repository fails closed (``jobs/failure_semantics.py`` classifies an unknown code
as TERMINAL; access control is deny-by-default), so this suite pins moderation to
the same rule and pins the parsing robustness that keeps failing closed rare.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.agents.phi_agent import (
    REASON_MODERATOR_ERROR,
    REASON_NON_BOOLEAN_VERDICT,
    REASON_UNPARSEABLE,
    PhiAgent,
    parse_moderation,
)
from nexus_ai_agent.llm.provider import LLMProvider


class ScriptedLLM(LLMProvider):
    """Returns a canned moderator reply so the parser is tested directly."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    async def generate(self, prompt: str, system: str | None = None) -> str:
        self.prompts.append(prompt)
        return self._reply

    async def embed(self, text: str) -> list[float]:
        _ = text
        return [0.0] * 384


class RaisingLLM(LLMProvider):
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def generate(self, prompt: str, system: str | None = None) -> str:
        raise self._exc

    async def embed(self, text: str) -> list[float]:
        _ = text
        return [0.0] * 384


# ── the contract: three states, and the third one refuses ────────────────


@pytest.mark.parametrize(
    ("reply", "expected_safe"),
    [
        ('{"safe": true, "reason": "ok"}', True),
        ('{"safe": false, "reason": "violence"}', False),
        ('```json\n{"safe": true, "reason": "ok"}\n```', True),
        ('Sure! {"safe": true, "reason": "ok"} Hope that helps.', True),
        ("true", True),
        ("false", False),
        ("safe: false because of the slur", False),
        ('{"safe":true}', True),
        ('{"reason": "ok", "safe": false}', False),
    ],
    ids=[
        "plain-safe",
        "plain-unsafe",
        "fenced",
        "embedded-in-prose",
        "bare-true",
        "bare-false",
        "keyed-in-prose",
        "no-reason",
        "reordered",
    ],
)
def test_a_verdict_that_was_actually_given_is_honoured(reply: str, expected_safe: bool) -> None:
    """Robust parsing first: failing closed must stay rare, not reflexive."""
    result = parse_moderation(reply)
    assert result["status"] == "verified"
    assert result["safe"] is expected_safe


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   ",
        "I think this content is generally fine and harmless.",
        "yes",
        "[no verdict here]",
        "{not json at all}",
        '{"safe": "yes"}',
        '{"safe": 1}',
        '{"safe": null}',
        '{"reason": "ok"}',
        "[1, 2, 3]",
        '"true-ish"',
    ],
    ids=[
        "empty",
        "whitespace",
        "prose-approval",
        "bare-yes",
        "bracketed",
        "malformed-object",
        "string-verdict",
        "integer-verdict",
        "null-verdict",
        "missing-verdict",
        "array",
        "quoted-true-ish",
    ],
)
def test_an_unestablished_verdict_is_refused_not_approved(reply: str) -> None:
    """The regression this suite exists for: no verdict means no approval."""
    result = parse_moderation(reply)
    assert result["status"] == "unverified"
    assert result["safe"] is False, "an unevaluated response must never be delivered as approved"


def test_prose_approval_is_the_exact_case_that_used_to_pass() -> None:
    """A model saying 'this is fine' in prose is not a parseable verdict.

    Under the old code this returned ``safe=True``. The words are reassuring and
    mean nothing: the gate did not establish a verdict, so it must not claim one.
    """
    result = parse_moderation("I think this content is generally fine and harmless.")
    assert result["safe"] is False
    assert result["reason"] == REASON_UNPARSEABLE


def test_non_boolean_verdict_gets_its_own_reason() -> None:
    """'The model answered, but not with a boolean' is a different outage."""
    result = parse_moderation('{"safe": "yes"}')
    assert result["safe"] is False
    assert result["reason"] == REASON_NON_BOOLEAN_VERDICT
    assert result["status"] == "unverified"


# ── end-to-end through the agent ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_moderate_returns_the_legacy_shape_callers_read() -> None:
    """``graph.py::moderation_node`` reads ``result.get("safe")``; that is stable."""
    agent = PhiAgent(ScriptedLLM('{"safe": true, "reason": "ok"}'))
    result = await agent.moderate("Hello world")
    assert "safe" in result
    assert result["safe"] is True


@pytest.mark.asyncio
async def test_moderate_fails_closed_when_the_moderator_is_unreachable() -> None:
    agent = PhiAgent(RaisingLLM(RuntimeError("provider unreachable")))
    result = await agent.moderate("Hello world")
    assert result["safe"] is False
    assert result["status"] == "unverified"
    assert str(result["reason"]).startswith(REASON_MODERATOR_ERROR)


@pytest.mark.asyncio
async def test_failure_reason_never_echoes_content_or_provider_text() -> None:
    """A provider can echo the prompt inside its own error text.

    Moderation sees the assistant's output, so leaking ``str(exc)`` would put
    generated content into an operational record. Only the exception *type* is
    recorded — the same boundary PR #119 holds for memory write failures.
    """
    secret = "My secret code is ALPHA-42"
    agent = PhiAgent(RaisingLLM(RuntimeError(f"upstream rejected: {secret}")))
    result = await agent.moderate(secret)

    reason = str(result["reason"])
    assert "ALPHA-42" not in reason
    assert "secret" not in reason.lower()
    assert "RuntimeError" in reason, "the type is enough to alert and correlate"


@pytest.mark.asyncio
async def test_moderation_is_applied_to_the_response_not_the_prompt() -> None:
    """Pins what the gate is for, so a future refactor cannot silently flip it."""
    llm = ScriptedLLM('{"safe": false, "reason": "slur"}')
    agent = PhiAgent(llm)
    await agent.moderate("candidate assistant output")
    assert llm.prompts and "candidate assistant output" in llm.prompts[0]


@pytest.mark.parametrize(
    "reply",
    [
        "this looks acceptable to me",
        "",
        '{"safe": "yes"}',
        "{not json at all}",
    ],
    ids=["prose", "empty", "non-boolean", "malformed"],
)
def test_the_graphs_own_decision_rule_now_refuses(reply: str) -> None:
    """Pin the interaction contract with the owner-leased graph.

    ``graph.py::moderation_node`` blocks with ``if not result.get("safe", True)``.
    That rule was fail-open *because of what moderate() returned*: a missing key
    defaulted to True and a parse error explicitly said True. The rule is now
    fail-closed without a single edit to the leased file, because the unverified
    state reports ``safe=False``. This test asserts that composition directly, so
    a change on either side of the seam is caught here rather than in production.
    """
    verdict = parse_moderation(reply)
    blocked = not verdict.get("safe", True)  # exactly what moderation_node does
    assert blocked is True


def test_a_verified_safe_verdict_still_passes_the_graphs_rule() -> None:
    """Fail-closed must not become fail-always, or the assistant refuses everything."""
    verdict = parse_moderation('{"safe": true, "reason": "ok"}')
    blocked = not verdict.get("safe", True)
    assert blocked is False
    assert verdict["status"] == "verified"
