"""Memory recall harness — wave-4 step4.

Pins a deterministic recall@k baseline on the long-term memory path.
Uses a stub LLM so the test is offline and free.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.memory.eval import (
    FIXTURE_QUERIES,
    evaluate_long_term_recall,
    is_regression,
    recall_at_k,
)


def test_recall_at_k_pure() -> None:
    ranked = [
        ["forces join via get_chat_member", "other"],
        ["slideshow FFmpeg video", "other"],
        ["R2 Cloudflare backups", "other"],
        ["python-telegram-bot Telegram API", "other"],
    ]
    assert recall_at_k(ranked, FIXTURE_QUERIES, k=1) == 1.0
    # Miss one
    ranked[0] = ["unrelated"]
    assert recall_at_k(ranked, FIXTURE_QUERIES, k=1) == 0.75


def test_recall_at_k_empty_queries() -> None:
    assert recall_at_k([], [], k=3) == 1.0


@pytest.mark.asyncio
async def test_evaluate_long_term_recall_is_deterministic() -> None:
    a = await evaluate_long_term_recall(k=3)
    b = await evaluate_long_term_recall(k=3)
    assert a == b
    assert 0 <= a <= 1.0
    # Baseline guard: must not regress >15 % from committed baseline (0.25
    # accommodates both sqlite-vec and fallback recency paths).
    assert not is_regression(a), f"recall@3 {a} regressed below baseline 0.25"


def test_is_regression_threshold() -> None:
    assert is_regression(0.5, baseline=0.75) is True  # 0.5 < 0.60
    assert is_regression(0.60, baseline=0.75) is False  # exactly at tolerance
    assert is_regression(0.75, baseline=0.75) is False
