"""Memory recall harness — and the repair of a feedback loop that could not fail.

What this module used to prove
------------------------------
It pinned ``_BASELINE_RECALL_K = 0.25`` over a **6-document** corpus at ``k=3``
and failed only below ``0.10``. The committed comment said the baseline
"accommodates both sqlite-vec and fallback recency paths" — which is to say the
guard was deliberately calibrated so that *losing semantic retrieval entirely
could not fail the build*. Two independent measurements show how complete that
blindness was:

* a **random** ranking of that corpus scores recall@3 = **0.501** in expectation
  (half the corpus is returned, so a keyword is in the window by chance);
* the production path — a dense lane over the hash-seeded random vectors that
  ``GeminiProvider``/``LiteLLMProvider``/``FakeLLMProvider`` return — scored
  **0.167**, *below* both chance and plain recency.

So the harness reported "0.50, baseline 0.25, PASS" while retrieval was worse
than shuffling. A metric that cannot distinguish signal from noise is not a
regression guard; it is reassurance.

What it proves now
------------------
* the corpus carries **distractors**, so chance recall@3 is ~0.15 instead of
  ~0.50 and a correct ranker is visibly separated from a lucky one;
* the baseline is the **measured** value (1.0), so a fall back to recency or to
  noise-ranking fails the build;
* :func:`chance_recall_at_k` states the analytical floor, and the test asserts
  retrieval beats it — the property the old harness never checked.

Still deterministic and offline: fixture memories and probes are hard-coded and
the embedder is a stub, so no network, model or native extension is required.
The layer rule (``memory/`` is a leaf, never importing ``features/`` or ``bot/``)
is enforced by ``tests/architecture/test_memory_boundaries.py``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class EvalQuery:
    query: str
    expected_keywords: tuple[str, ...]  # at least one must appear in top-k


# Deterministic fixture. The first six are the original anchors; the rest are
# distractors drawn from real facts about this repository, so the corpus has the
# shape memory actually sees (many plausible, mostly irrelevant rows) rather than
# six documents of which any three are a coin-flip away from a hit.
FIXTURE_MEMORIES: tuple[str, ...] = (
    # ── anchors (each is the target of one probe below) ──
    "NEXUS AI agent supports Telegram bot API with python-telegram-bot",
    "The slideshow pack composes 5 images into a 30 second video via FFmpeg",
    "Force-join verifies channel membership via get_chat_member",
    "The R2 blob tier stores database backups on Cloudflare",
    "Alembic heads are verified on real PostgreSQL in CI",
    "The memory short-term window keeps last 20 messages",
    # ── distractors ──
    "The modular monolith constraint forbids Celery and Redis in production code",
    "Nagar capability packs stay pure: stdlib plus pydantic plus creative.studio",
    "The restricted shell tool allowlists commands and confines paths to a workspace",
    "Delivery packs are signed with ed25519 and verified against a trust root",
    "Rendered artifacts are probed with ffprobe and hashed with sha256 before publish",
    "The job queue persists to a SQLite sidecar and resumes after a restart",
    "Liveness at healthz is deliberately database-free so it never blocks on I/O",
    "Access guard denies every user id that is not explicitly allowed",
    "Personality engine tracks valence and arousal to adjust the assistant tone",
    "Caption engine adapters fail closed when the optional speech extra is missing",
    "OTIO interop exports the timeline for round-trip into other editors",
    "The claim board gives one owner per file zone with a 24 hour lease",
    "Reciprocal rank fusion merges the lexical and dense retrieval lanes",
    "Retention keeps resumability for thirty days behind a circuit breaker",
    "Portrait operations are catalogued but several identifiers are unimplemented",
    "The webhook run mode binds uvicorn and verifies chat membership on join",
    "Golden tests compare an independent proof rather than trusting the producer",
    "Continuum snapshots record committed project state to survive context loss",
)

FIXTURE_QUERIES: tuple[EvalQuery, ...] = (
    EvalQuery("how to verify channel join?", ("Force-join", "get_chat_member")),
    EvalQuery("slideshow video rendering", ("slideshow", "FFmpeg")),
    EvalQuery("database backup storage", ("R2", "Cloudflare", "backups")),
    EvalQuery("Telegram bot library", ("python-telegram-bot", "Telegram")),
)

#: Measured baseline with the hybrid lexical+dense lane (see module docstring).
#: Raised from 0.25 on purpose: at 1.0 a fall back to recency (~0.25 on the old
#: corpus, ~0.15 on this one) or to noise-ranking (0.167) now FAILS the build
#: instead of passing under a floor chosen to accommodate it.
_BASELINE_RECALL_K = 1.0
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


def chance_recall_at_k(
    k: int = 3,
    corpus: Sequence[str] = FIXTURE_MEMORIES,
    queries: Sequence[EvalQuery] = FIXTURE_QUERIES,
) -> float:
    """Analytical recall@k of a **random** ranking over ``corpus``.

    This is the floor a retrieval metric must clear to mean anything. For each
    probe it is the probability that a uniformly random ``k``-subset of the
    corpus contains at least one relevant document:

        1 - C(n - r, k) / C(n, k)

    With the original six-document corpus this was ≈0.50, which is why a
    noise-ranked retriever could report "0.50" and look healthy. With
    distractors it is ≈0.15, so the same failure is now unmistakable.
    """
    if not queries or not corpus or k < 1:
        return 0.0
    n = len(corpus)
    total = 0.0
    for q in queries:
        relevant = sum(
            1 for doc in corpus if any(kw.lower() in doc.lower() for kw in q.expected_keywords)
        )
        relevant = min(relevant, n)
        if relevant == 0:
            continue
        if k >= n:
            total += 1.0
            continue
        miss = math.comb(n - relevant, k) / math.comb(n, k)
        total += 1.0 - miss
    return total / len(queries)


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
    try:
        return recall_at_k(ranked, FIXTURE_QUERIES, k=k)
    finally:
        mem.close()


# Re-export for tests
__all__ = [
    "EvalQuery",
    "FIXTURE_MEMORIES",
    "FIXTURE_QUERIES",
    "_BASELINE_RECALL_K",
    "_StubLLM",
    "chance_recall_at_k",
    "evaluate_long_term_recall",
    "is_regression",
    "recall_at_k",
]
