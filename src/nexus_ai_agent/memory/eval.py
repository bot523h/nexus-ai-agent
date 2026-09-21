"""Memory recall harness — wave-4 step4.

A tiny, deterministic harness that measures ``recall@k`` for the long-term
memory retrieval path.  It is *not* a new retrieval engine — it wraps the
existing :class:`LongTermMemory` so we can pin a baseline and guard
regressions in CI.

The harness is:

* **Deterministic.**  Fixture memories + queries are hard-coded; the LLM
  embedding is stubbed (``length % 3``) so the test never needs a network
  or a model.

* **Offline.**  No chroma, no torch, no external service — the same
  SQLite store the production code uses, plus a fake ``embed``.

* **Architecture-guarded.**  The separate test
  ``test_memory_boundaries.py`` asserts that ``memory/`` never imports
  ``features/`` or ``bot/`` (the layer rule: memory is a leaf).

The baseline is committed as ``_BASELINE_RECAL_K``: if recall drops
by more than 15 % the test fails — the same pattern as the render-bench
threshold in wave-4 step6.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class EvalQuery:
    query: str
    expected_keywords: tuple[str, ...]  # at least one must appear in top-k


# Deterministic fixture: 6 memories anchored to one thread
FIXTURE_MEMORIES: tuple[str, ...] = (
    "NEXUS AI agent supports Telegram bot API with python-telegram-bot",
    "The slideshow pack composes 5 images into a 30 second video via FFmpeg",
    "Force-join verifies channel membership via get_chat_member",
    "The R2 blob tier stores database backups on Cloudflare",
    "Alembic heads are verified on real PostgreSQL in CI",
    "The memory short-term window keeps last 20 messages",
)

FIXTURE_QUERIES: tuple[EvalQuery, ...] = (
    EvalQuery("how to verify channel join?", ("Force-join", "get_chat_member")),
    EvalQuery("slideshow video rendering", ("slideshow", "FFmpeg")),
    EvalQuery("database backup storage", ("R2", "Cloudflare", "backups")),
    EvalQuery("Telegram bot library", ("python-telegram-bot", "Telegram")),
)

_BASELINE_RECALL_K = 0.5  # committed baseline (observed 2/4 with stub embedding)
_REGRESSION_TOLERANCE = 0.15  # fail if >15 % below baseline


def recall_at_k(
    results: Sequence[Sequence[str]], queries: Sequence[EvalQuery], k: int = 3
) -> float:
    """Compute recall@k over ``results`` (one ranked list per query)."""
    if not queries:
        return 1.0
    hits = 0
    for ranked, q in zip(results, queries, strict=True):
        top_k = ranked[:k]
        # Hit if any expected keyword appears (case-insensitive substring)
        if any(any(kw.lower() in doc.lower() for doc in top_k) for kw in q.expected_keywords):
            hits += 1
    return hits / len(queries)


def is_regression(current: float, baseline: float = _BASELINE_RECALL_K) -> bool:
    return current < (baseline - _REGRESSION_TOLERANCE)


# -- stub LLM for offline eval -------------------------------------------


class _StubLLM:
    """Deterministic stub: embedding = 384-dim vector where dim d = (hash(text)+d) % 1.0."""

    async def embed(self, text: str) -> list[float]:
        # Cheap deterministic embedding: use text length + char sum
        base = sum(ord(c) for c in text) % 100
        return [((base + i) % 10) / 10.0 for i in range(384)]

    async def generate(self, prompt: str, system: str | None = None) -> str:  # noqa: ARG002
        return "stub"


# -- harness entry point ---------------------------------------------------


async def evaluate_long_term_recall(k: int = 3) -> float:
    """Store the fixture, query, and return recall@k (offline, deterministic)."""
    from nexus_ai_agent.memory.long_term import LongTermMemory

    mem = LongTermMemory(":memory:", _StubLLM())  # type: ignore[arg-type]
    thread = "eval-fixture"
    for m in FIXTURE_MEMORIES:
        await mem.store(thread, m)
    ranked: list[list[str]] = []
    for q in FIXTURE_QUERIES:
        hits = await mem.search(thread, q.query, top_k=k)
        ranked.append(hits)
    return recall_at_k(ranked, FIXTURE_QUERIES, k=k)


# Re-export for tests
__all__ = [
    "EvalQuery",
    "FIXTURE_MEMORIES",
    "FIXTURE_QUERIES",
    "recall_at_k",
    "is_regression",
    "evaluate_long_term_recall",
]
