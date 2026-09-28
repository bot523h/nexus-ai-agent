"""Short-term context window — the single owner of "how much conversation".

Why this module exists (it was dead code before)
-----------------------------------------------
``ShortTermMemory`` had **zero callers**. Meanwhile the conversation window was
re-implemented inline in six places with four different sizes:

======================================  =====
site                                    window
======================================  =====
``agents/phi_agent.py``                 last 8
``agents/qwen_agent.py``                last 10
``agents/chat_agent.py``                last 10
``agents/gemma_agent.py``               last 12
``orchestration/graph.py::_chat_agent`` last 10
``memory/short_term.py::summarize``     last 10
======================================  =====

Two consequences, both real:

* **The assistant's context depended on which persona a keyword matcher
  happened to select.** ``select_persona`` routes on ~50 literals; a user whose
  ninth message back held the answer got it from Gemma and not from Phi. Same
  conversation, same store, different visible history — semantic ambiguity with
  no documented rationale anywhere for the 8/10/12 split.
* **Three settings were decoration.** ``max_short_term_messages`` (20),
  ``max_tokens_before_summary`` (3000) and ``top_k_memories`` (3) are declared in
  ``config/settings.py`` and read by nothing; each had a hardcoded twin in code.
  An operator setting ``NEXUS_MAX_SHORT_TERM_MESSAGES`` changed no behaviour.

``get_window`` also ignored its own constant — it returned ``messages[-20:]``
rather than ``messages[-self.MAX_MESSAGES:]``, so even the dead class disagreed
with itself.

The policy now lives here once, is bounded by **both** declared limits (count and
token budget), and is what every persona renders through. This module stays a
pure leaf: it takes its limits as arguments and never imports ``config``,
``features`` or ``bot`` (``tests/architecture/test_memory_boundaries.py``). The
composition-aware side — reading settings — lives in ``agents/base.py``.
"""

from __future__ import annotations

from nexus_ai_agent.llm.provider import LLMProvider

#: Approximate characters per token, matching ``retrieval.core.estimate_tokens``.
#: A byte-pair tokenizer would cost a heavy dependency; this proxy is
#: deterministic and only ever used to *bound* a window, never to bill for it.
_CHARS_PER_TOKEN = 4


class ShortTermMemory:
    """The conversation window policy: what a turn is allowed to see."""

    #: Defaults mirror the long-dead ``config/settings.py`` values so that a
    #: caller which passes nothing gets the documented intent, not a new number.
    MAX_MESSAGES = 20
    MAX_TOKENS_BEFORE_SUMMARY = 3000

    def __init__(
        self,
        max_messages: int | None = None,
        max_tokens_before_summary: int | None = None,
    ) -> None:
        self.max_messages = self.MAX_MESSAGES if max_messages is None else max(1, int(max_messages))
        self.max_tokens_before_summary = (
            self.MAX_TOKENS_BEFORE_SUMMARY
            if max_tokens_before_summary is None
            else max(1, int(max_tokens_before_summary))
        )

    # ── the window ──────────────────────────────────────────────────────

    def get_window(self, messages: list[dict]) -> list[dict]:
        """The messages one turn may see: newest ``max_messages``, budget-trimmed.

        Two bounds, both declared, both previously dead. The count bound is the
        familiar one; the token bound is what stops "raise the window to 20" from
        silently becoming an unbounded prompt on a long conversation. Trimming
        drops the **oldest** messages and always keeps the newest one, so a single
        very long message can never produce an empty window.
        """
        if not messages:
            return []
        window = messages[-self.max_messages :]
        while len(window) > 1 and self.estimate_tokens(window) > self.max_tokens_before_summary:
            window = window[1:]
        return window

    @staticmethod
    def estimate_tokens(messages: list[dict]) -> int:
        """Deterministic token estimate for a window (≈ 4 characters per token)."""
        if not messages:
            return 0
        characters = sum(len(str(message.get("content") or "")) for message in messages)
        return max(1, -(-characters // _CHARS_PER_TOKEN))

    def render(self, messages: list[dict]) -> str:
        """The window as prompt text — the one copy of this ``role: content`` join.

        This expression was duplicated in six places with six different slices.
        Rendering and windowing are deliberately one operation: a caller that
        could slice independently of the policy is how the four sizes appeared.
        """
        return "\n".join(
            f"{message.get('role', '')}: {message.get('content', '')}"
            for message in self.get_window(messages)
        )

    # ── summarisation ───────────────────────────────────────────────────

    def should_summarize(self, messages: list[dict]) -> bool:
        """Whether the window has outgrown the token budget.

        Synchronous, and deliberately so: it was previously ``async`` while doing
        no I/O at all, which forced a pointless ``await`` on a pure predicate and
        hid the fact that nothing was being waited for.
        """
        return self.estimate_tokens(messages) > self.max_tokens_before_summary

    async def summarize(self, messages: list[dict], llm: LLMProvider) -> str:
        """Compress the window through ``llm``.

        Summarises the *same* window the personas see. It previously sliced its
        own hardcoded ``[-10:]``, so the summary could cover messages the reply
        never saw, or miss ones it did.
        """
        text = self.render(messages)
        if not text:
            return ""
        prompt = f"Summarize this conversation briefly:\n{text}"
        return await llm.generate(prompt, system="You are a concise summarizer.")


__all__ = ["ShortTermMemory"]
