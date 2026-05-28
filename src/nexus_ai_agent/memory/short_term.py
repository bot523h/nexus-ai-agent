from __future__ import annotations

from nexus_ai_agent.llm.provider import LLMProvider


class ShortTermMemory:
    MAX_MESSAGES = 20
    MAX_TOKENS_BEFORE_SUMMARY = 3000

    def get_window(self, messages: list[dict]) -> list[dict]:
        return messages[-self.MAX_MESSAGES :]

    async def should_summarize(self, messages: list[dict]) -> bool:
        est_tokens = sum(len(m.get("content", "")) for m in messages) / 4
        return est_tokens > self.MAX_TOKENS_BEFORE_SUMMARY

    async def summarize(self, messages: list[dict], llm: LLMProvider) -> str:
        prompt = (
            "Summarize the following conversation for long-term memory.\n\n"
            + "\n".join([f"{m.get('role')}: {m.get('content')}" for m in messages])
        )
        return await llm.generate(prompt=prompt, system="You summarize conversations succinctly.")

