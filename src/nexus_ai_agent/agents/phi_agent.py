"""Logic persona, and the output-moderation gate.

The moderation contract, and why it changed
-------------------------------------------
``PhiAgent.moderate`` is the last filter between the assistant's generated text
and the user. It used to be:

    try:
        return json.loads(raw)
    except Exception:
        return {"safe": True, "reason": "parse_error"}

That is **fail-open**: a model that answered ``"yes, that's fine"`` instead of
JSON — or was cut off mid-token, or wrapped the JSON in prose — was treated as a
*pass*. The one component whose entire job is to refuse unsafe output refused
nothing exactly when it could not understand its own input. The failure was also
silent and self-contradicting: ``reason="parse_error"`` alongside ``safe=True``
reports "I could not evaluate this" in the field that means "I evaluated it and
it is fine".

The rest of this repository does the opposite, deliberately.
``jobs/failure_semantics.py`` classifies an unknown typed code as TERMINAL
because "fail closed toward visibility, never silent retry-spam"; access control
is deny-by-default (Q2). Moderation was the one gate that failed open.

It now returns a three-state answer and fails closed on the third:

``status="verified"``
    the model's verdict was parsed; ``safe`` is its answer.
``status="unverified"``
    the verdict could not be established (unparseable, non-boolean, or the call
    raised). ``safe`` is ``False`` and ``reason`` names what went wrong.

Parsing is made genuinely robust first — fenced blocks, JSON embedded in prose,
a bare ``true``/``false`` — so failing closed stays rare rather than becoming a
usability problem. What must never happen again is an *unevaluated* response
being delivered as an *approved* one.

Callers read ``result["safe"]`` exactly as before
(``orchestration/graph.py::moderation_node``), so no call site changes; the added
keys are there so an operator can tell "blocked because unsafe" from "blocked
because the moderator could not answer" — which is the difference between a
content problem and an outage.
"""

from __future__ import annotations

import json
import re

from nexus_ai_agent.agents.base import BaseAgent
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.personality.engine import PersonalityEngine

#: What the moderator must answer with. Kept terse on purpose: the shorter the
#: required output, the less room a model has to wander out of the format.
_MODERATION_SYSTEM = 'Reply ONLY with JSON: {"safe": true, "reason": "ok"}'

#: Reasons reported when a verdict could not be established. A typed vocabulary
#: rather than free text, so an operator can alert on one of them.
REASON_UNPARSEABLE = "moderation_unparseable"
REASON_NON_BOOLEAN_VERDICT = "moderation_non_boolean_verdict"
REASON_MODERATOR_ERROR = "moderation_error"
REASON_OK = "ok"
REASON_UNSAFE = "unsafe"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
#: A bare verdict, and *only* a bare verdict: the whole reply, modulo
#: surrounding punctuation. Anchored on purpose — an unanchored ``\b(true)\b``
#: reads a verdict into ``"true-ish"``, ``"not false"`` or any sentence that
#: happens to contain the word, which is precisely the fail-open shape this
#: module exists to remove. A boolean embedded in prose is not a verdict; a
#: ``"safe": <bool>`` pair is, and is matched by :data:`_SAFE_KEY_RE` instead.
_BOOL_RE = re.compile(r"^\W*(true|false)\W*$", re.IGNORECASE)
_SAFE_KEY_RE = re.compile(r'"?safe"?\s*[:=]\s*(true|false)', re.IGNORECASE)


def _candidate_payloads(raw: str) -> list[str]:
    """Every plausible spelling of the JSON object, most explicit first."""
    text = (raw or "").strip()
    if not text:
        return []
    candidates = [text]
    candidates.extend(match.group(1).strip() for match in _FENCE_RE.finditer(text))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def parse_moderation(raw: str) -> dict[str, object]:
    """Parse a moderator reply into the three-state verdict contract.

    Never raises. Anything it cannot establish a verdict from comes back
    ``status="unverified"`` with ``safe=False`` — an unevaluated response is not
    an approved one.
    """
    for candidate in _candidate_payloads(raw):
        try:
            payload = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        verdict = payload.get("safe")
        if isinstance(verdict, bool):
            reason = payload.get("reason")
            return {
                "safe": verdict,
                "reason": str(reason)
                if reason is not None
                else (REASON_OK if verdict else REASON_UNSAFE),
                "status": "verified",
            }
        if verdict is not None:
            # The model answered, but not with a boolean. That is not a pass.
            return {
                "safe": False,
                "reason": REASON_NON_BOOLEAN_VERDICT,
                "status": "unverified",
            }

    # No JSON object at all. Accept an unambiguous bare verdict as a last resort
    # rather than refusing a moderation that plainly succeeded.
    keyed = _SAFE_KEY_RE.search(raw or "")
    bare = _BOOL_RE.search(raw or "")
    match = keyed or bare
    if match:
        verdict = match.group(1).lower() == "true"
        return {
            "safe": verdict,
            "reason": REASON_OK if verdict else REASON_UNSAFE,
            "status": "verified",
        }
    return {"safe": False, "reason": REASON_UNPARSEABLE, "status": "unverified"}


class PhiAgent(BaseAgent):
    """Logic, moderation, structured analysis."""

    def __init__(self, llm: LLMProvider, state_path: str | None = None) -> None:
        super().__init__(llm)
        self._pe = PersonalityEngine("phi", state_path)

    async def run(self, state: NexusState) -> NexusState:
        msgs = state.get("messages", [])
        last = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        self._pe.update(last)
        system = self._pe.build_system_prompt(
            "You analyze and reason logically. Structure answers clearly. " + self._pe.style_hint(),
            state.get("memory_context", ""),
        )
        conv = self.render_conversation(state)
        resp = await self.llm.generate(conv + "\nassistant:", system=system)
        return {**state, "response": resp, "active_persona": "phi"}

    async def moderate(self, text: str) -> dict:
        """Judge whether ``text`` may be shown to the user. Fails closed.

        Returns ``{"safe": bool, "reason": str, "status": "verified"|"unverified"}``.
        A transport failure and an unparseable verdict are both ``unverified``,
        distinguished only by ``reason`` so an operator can tell an outage from a
        model that stopped following its format.
        """
        try:
            raw = await self.llm.generate(
                f"Is this content safe?\n{text}", system=_MODERATION_SYSTEM
            )
        except Exception as exc:
            # The moderator could not be reached. Nothing was evaluated, so
            # nothing is approved. The exception *type* is recorded, never its
            # text: a provider can echo the content it was asked about.
            return {
                "safe": False,
                "reason": f"{REASON_MODERATOR_ERROR}:{type(exc).__name__}",
                "status": "unverified",
            }
        return parse_moderation(raw)
