"""PHASE 3 — provider identity: ``LLMProvider.generate`` carries a real principal.

The ``user_id=0`` sentinel in ``GeminiProvider.generate`` collapsed every
graph/persona turn into one shared quota bucket and silently dropped the
caller identity.  Contract under test:

- ``generate(..., user_id=<real id>)`` forwards that id to the quota seam
  (``ask``) — two users can never collapse into one bucket through the
  provider layer;
- ``generate()`` without an identity uses the explicit type-safe system
  principal (``SYSTEM_PRINCIPAL_ID = -1``) — never a magic ``0``;
- an explicit ``user_id=0`` is API misuse and raises (no compatibility path
  accepts or produces the sentinel);
- every ``LLMProvider`` implementation accepts the keyword (coherent surface);
- persona agents forward the state's identity to ``generate`` (0/None maps to
  the system principal, a real id is preserved).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.features.ai_chat import GeminiEngine
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.fallback_provider import FallbackProvider
from nexus_ai_agent.llm.gemini_provider import GeminiProvider
from nexus_ai_agent.llm.litellm_provider import LiteLLMRoutingProvider
from nexus_ai_agent.llm.local_llama_cpp import LocalLlamaCppProvider
from nexus_ai_agent.llm.local_server_provider import LocalLlamaServerProvider
from nexus_ai_agent.llm.provider import SYSTEM_PRINCIPAL_ID

ROOT = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent"


class _EngineSpy:
    """Stands in for GeminiEngine: records the principal ask() was called with."""

    def __init__(self) -> None:
        self.seen: list[int | None] = []

    async def ask(self, text: str, *, user_id: int) -> str:  # noqa: ARG002
        self.seen.append(user_id)
        return "ok"


def _provider(engine: _EngineSpy) -> GeminiProvider:
    p = GeminiProvider.__new__(GeminiProvider)
    p._engine = engine  # type: ignore[attr-defined]
    return p


@pytest.mark.asyncio
async def test_generate_forwards_real_identity_to_quota_seam() -> None:
    engine = _EngineSpy()
    p = _provider(engine)
    await p.generate("hello", user_id=4242)
    await p.generate("hello", user_id=9876)
    assert engine.seen == [4242, 9876], (
        f"generate() must forward each caller's real identity to ask(); saw {engine.seen!r}"
    )


@pytest.mark.asyncio
async def test_generate_without_identity_uses_system_principal() -> None:
    engine = _EngineSpy()
    p = _provider(engine)
    await p.generate("hello")
    assert engine.seen == [SYSTEM_PRINCIPAL_ID], (
        f"identity-less generate() must meter as SYSTEM principal "
        f"({SYSTEM_PRINCIPAL_ID}), never the 0 sentinel: saw {engine.seen!r}"
    )
    assert SYSTEM_PRINCIPAL_ID != 0


@pytest.mark.asyncio
async def test_generate_rejects_explicit_zero_identity() -> None:
    engine = _EngineSpy()
    p = _provider(engine)
    with pytest.raises(ValueError):
        await p.generate("hello", user_id=0)
    assert engine.seen == [], "a rejected identity must never reach the quota seam"


def test_every_provider_accepts_user_id_keyword() -> None:
    """Coherent API surface: all LLMProvider implementations take user_id=None."""
    impls = [
        FakeLLMProvider,
        FallbackProvider,
        GeminiProvider,
        LiteLLMRoutingProvider,
        LocalLlamaCppProvider,
        LocalLlamaServerProvider,
    ]
    for cls in impls:
        sig = inspect.signature(cls.generate)
        assert "user_id" in sig.parameters, f"{cls.__name__}.generate lacks user_id"
        param = sig.parameters["user_id"]
        assert param.default is None, f"{cls.__name__}.generate user_id must default to None"


@pytest.mark.asyncio
async def test_persona_agents_forward_state_identity() -> None:
    """Persona turns must carry the speaking user's id into generate()."""
    from nexus_ai_agent.agents.gemma_agent import GemmaAgent

    class _IdSpy(FakeLLMProvider):
        def __init__(self) -> None:
            super().__init__()
            self.ids: list[int | None] = []

        async def generate(self, prompt: str, system: str = "", **kwargs: Any) -> str:
            self.ids.append(kwargs.get("user_id"))
            return await super().generate(prompt, system, **kwargs)

    llm = _IdSpy()
    agent = GemmaAgent(llm)
    base = {
        "thread_id": "t",
        "chat_id": 4242,
        "correlation_id": "c",
        "messages": [{"role": "user", "content": "hi"}],
        "intent": "chat",
        "active_persona": "gemma",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }
    await agent.run({**base, "user_id": 4242})
    assert llm.ids == [4242], f"persona dropped the caller identity: {llm.ids!r}"
    await agent.run({**base, "user_id": 0})
    assert llm.ids[-1] is None, (
        f"persona mapped the 0 sentinel to {llm.ids[-1]!r}; must be None (system principal)"
    )


def test_vision_requires_explicit_identity() -> None:
    """``vision()`` must not default the identity to the 0 sentinel."""
    sig = inspect.signature(GeminiEngine.vision)
    param = sig.parameters["user_id"]
    assert param.default is inspect.Parameter.empty, (
        f"vision() still defaults user_id to {param.default!r} — the sentinel is back"
    )


def test_no_user_id_zero_sentinel_in_identity_surface() -> None:
    """No compatibility path may produce/meter with the 0 sentinel.

    Scans the provider layer and the Gemini engine surface for the removed
    sentinel **in code** (string literals and comments describing the ban are
    fine — only real sentinel assignments/calls count).  (bot/handlers.py is
    covered by task-p1c's fail-closed work.)
    """
    import io
    import tokenize

    offenders: list[str] = []
    targets = [
        ROOT / "llm",
        ROOT / "features" / "ai_chat.py",
    ]
    for target in targets:
        files = [target] if target.is_file() else sorted(target.rglob("*.py"))
        for f in files:
            src = f.read_text(encoding="utf-8")
            tokens = [
                tok
                for tok in tokenize.generate_tokens(io.StringIO(src).readline)
                if tok.type == tokenize.NAME
                or tok.type == tokenize.OP
                or tok.type == tokenize.NUMBER
            ]
            # Pattern in code tokens: user_id = 0  (spacing/keywords ignored).
            for i in range(len(tokens) - 2):
                a, b, c = tokens[i], tokens[i + 1], tokens[i + 2]
                if a.string == "user_id" and b.string == "=" and c.string == "0":
                    line = src.splitlines()[a.start[0] - 1].strip()
                    offenders.append(f"{f.relative_to(ROOT)}:{a.start[0]}: {line}")
    assert offenders == [], (
        "the user_id=0 sentinel survived in the identity surface:\n" + "\n".join(offenders)
    )


@pytest.mark.asyncio
async def test_moderate_forwards_caller_identity() -> None:
    """Moderation runs per user turn — it must bill that user, not the system.

    (CodeRabbit 4238943208: dropping the identity here meters user turns on
    the shared system principal.)
    """
    from nexus_ai_agent.agents.phi_agent import PhiAgent

    class _IdSpy(FakeLLMProvider):
        def __init__(self) -> None:
            super().__init__()
            self.ids: list[int | None] = []

        async def generate(self, prompt: str, system: str = "", **kwargs: Any) -> str:
            self.ids.append(kwargs.get("user_id"))
            return '{"safe": true, "reason": "ok"}'

    llm = _IdSpy()
    phi = PhiAgent(llm)
    await phi.moderate("hello", user_id=4242)
    assert llm.ids == [4242], f"moderate() dropped the caller identity: {llm.ids!r}"
    await phi.moderate("hello")
    assert llm.ids[-1] is None, (
        f"identity-less moderate() must map to the system principal: {llm.ids!r}"
    )
