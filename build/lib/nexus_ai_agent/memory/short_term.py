from __future__ import annotations

from nexus_ai_agent.llm.provider import LLMProvider


class ShortTermMemory:
    MAX_MESSAGES = 20
    MAX_TOKENS_BEFORE_SUMMARY = 3000

    def get_window(self, messages: list[dict]) -> list[dict]:
        return messages[-20:]

    async def should_summarize(self, messages: list[dict]) -> bool:
        total = sum(len(m["content"]) for m in messages) / 4
        return total > self.MAX_TOKENS_BEFORE_SUMMARY

    async def summarize(self, messages: list[dict], llm: LLMProvider) -> str:
        text = "\n".join(f"{m['role']}: {m['content']}" for m in messages[-10:])
        prompt = f"Summarize this conversation briefly:\n{text}"
        return await llm.generate(prompt, system="You are a concise summarizer.")
