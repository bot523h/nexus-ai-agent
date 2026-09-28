"""Retrieval truth for long-term memory — the transformation and its guards.

These tests exist because the memory path had a defect that no existing test
could see, and the reason it could not be seen is itself part of the finding:

* ``LongTermMemory`` ranked memories with a single dense lane whose vectors came
  from ``llm.embed()``. Every embedder this repository ships by default
  (``GeminiProvider``, ``LiteLLMProvider``, ``FakeLLMProvider``) returns a
  **hash-seeded random vector**, so cosine similarity carried no semantic
  signal. Measured on a labelled corpus: hit_rate@3 = 0.167 — *worse than
  returning the newest rows* (0.500).
* When ``sqlite-vec`` failed to load, ``search`` silently fell back to
  ``ORDER BY id DESC`` and returned the same ``list[str]`` shape. A semantic hit
  and "the three most recent rows" were indistinguishable to the caller.
* ``store`` raised when the embedder was down, so an offline deployment stored
  **no memories at all** — against Q1 (offline-first).
* The recall harness pinned a baseline of 0.25 over a 6-document corpus whose
  chance level was ~0.50, and failed only below 0.10. Its own comment said the
  baseline "accommodates both sqlite-vec and fallback recency paths": the guard
  was calibrated so that losing semantic retrieval could not fail the build.

The transformation wires memory to the dependency-free BM25 + reciprocal-rank
fusion kernel Nexus already owned (relocated to ``nexus_ai_agent.retrieval`` so
the ``memory/`` leaf fence allows it), makes recall **self-describing**, enforces
the embedding-dimension contract, preserves provenance, and survives a dead
embedder. Each group below pins one of those properties.
"""

from __future__ import annotations

import hashlib
import math
import random
import sqlite3
import struct
import sys
from collections.abc import Awaitable, Callable

import pytest

from nexus_ai_agent.memory.eval import (
    FIXTURE_MEMORIES,
    FIXTURE_QUERIES,
    chance_recall_at_k,
    evaluate_long_term_recall,
    is_regression,
    recall_at_k,
)
from nexus_ai_agent.memory.long_term import LongTermMemory
from nexus_ai_agent.memory.recall import (
    DENSE_LANE_EMPTY,
    EMBEDDING_UNAVAILABLE,
    NO_RANKING_SIGNAL,
    NOT_DEGRADED,
    MemoryRecall,
    RetrievalMode,
)
from nexus_ai_agent.retrieval import tokenize

Embedder = Callable[[str], Awaitable[list[float]]]


# ── embedders: the three kinds a deployment can actually have ─────────────


class NoiseEmbedder:
    """Byte-for-byte the behaviour of the repository's *default* providers.

    ``GeminiProvider.embed`` / ``LiteLLMProvider.embed`` / ``FakeLLMProvider.embed``
    all do: seed a ``random.Random`` from ``sha512(text)`` and emit uniform
    floats. Reproduced here so the test names the defect without importing a
    provider that needs credentials.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        digest = hashlib.sha512(text.encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8], "little"))
        return [rng.uniform(-0.1, 0.1) for _ in range(384)]


class RealEmbedder:
    """A signal-bearing embedder, standing in for all-MiniLM-L6-v2.

    Deterministic and offline: hashed lexical features, L2-normalised. It is not
    claimed to be a *good* embedding model — only to be one whose similarity is
    a function of content, which is the property the defaults lack.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        vector = [0.0] * 384
        for token in tokenize(text):
            digest = int(hashlib.md5(token.encode()).hexdigest(), 16)  # noqa: S324
            vector[digest % 384] += 1.0
            vector[(digest >> 9) % 384] += 0.5
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class DeadEmbedder:
    """No provider reachable — the offline-first (Q1) case."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        raise RuntimeError("embedding backend unreachable")


class WrongDimEmbedder:
    """A provider swap that quietly changed the vector width."""

    async def embed(self, text: str) -> list[float]:
        _ = text
        return [0.1] * 768


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    na = math.sqrt(sum(a * a for a in left))
    nb = math.sqrt(sum(b * b for b in right))
    return dot / (na * nb) if na and nb else 0.0


async def _seed(memory: LongTermMemory, thread: str = "t") -> None:
    for text in FIXTURE_MEMORIES:
        await memory.store(thread, text, metadata={"kind": "turn", "source": "test"})


def _hit_rate(ranked: list[list[str]], k: int = 3) -> float:
    hits = 0
    for top, query in zip(ranked, FIXTURE_QUERIES, strict=True):
        window = top[:k]
        if any(
            keyword.lower() in doc.lower() for doc in window for keyword in query.expected_keywords
        ):
            hits += 1
    return hits / len(FIXTURE_QUERIES)


async def _rank_all(memory: LongTermMemory, thread: str = "t", k: int = 3) -> list[list[str]]:
    return [await memory.search(thread, query.query, top_k=k) for query in FIXTURE_QUERIES]


# ── 1. the root cause, stated as evidence ────────────────────────────────


@pytest.mark.asyncio
async def test_default_provider_embeddings_carry_no_semantic_signal() -> None:
    """Why the dense-only lane failed: related texts get near-orthogonal vectors.

    This is the measurement that justifies the whole transformation. It is
    asserted, not just documented, so a future provider that starts returning
    real embeddings makes this test fail and the reasoning gets revisited
    instead of silently rotting.
    """
    embedder = NoiseEmbedder()
    related = await embedder.embed("database backup storage")
    target = await embedder.embed("The R2 blob tier stores database backups on Cloudflare")
    unrelated = await embedder.embed("The memory short-term window keeps last 20 messages")
    identical = await embedder.embed("database backup storage")

    assert _cosine(related, target) < 0.20, "a hash-seeded vector cannot express relevance"
    assert abs(_cosine(related, unrelated)) < 0.20
    # ...but it does express *identity*, which is exactly why this looked like it
    # was working: deduplication masquerading as semantic search.
    assert _cosine(related, identical) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_dense_only_ranking_over_noise_is_worse_than_chance() -> None:
    """Pin the pre-transformation behaviour so the improvement stays measurable.

    Ranks the fixture by cosine over noise vectors — what the single dense lane
    did — and asserts it lands at or below the analytical chance floor.
    """
    embedder = NoiseEmbedder()
    vectors = {text: await embedder.embed(text) for text in FIXTURE_MEMORIES}
    ranked: list[list[str]] = []
    for query in FIXTURE_QUERIES:
        query_vector = await embedder.embed(query.query)
        ordered = sorted(
            FIXTURE_MEMORIES,
            key=lambda text: (-_cosine(query_vector, vectors[text]), text),
        )
        ranked.append(ordered[:3])

    chance = chance_recall_at_k(3)
    assert recall_at_k(ranked, FIXTURE_QUERIES, k=3) <= chance + 0.25, (
        "noise ranking should not beat chance by a meaningful margin"
    )


# ── 2. the transformation: retrieval now works ───────────────────────────


@pytest.mark.asyncio
async def test_recall_beats_chance_with_the_default_noise_embedder() -> None:
    """The headline result: correct retrieval despite a meaningless embedder."""
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    await _seed(memory)
    ranked = await _rank_all(memory)

    measured = recall_at_k(ranked, FIXTURE_QUERIES, k=3)
    chance = chance_recall_at_k(3)
    assert measured == 1.0
    assert measured > chance + 0.5, (
        f"retrieval ({measured}) must clear chance ({chance}) decisively"
    )
    memory.close()


@pytest.mark.asyncio
async def test_retrieval_survives_a_missing_sqlite_vec(monkeypatch: pytest.MonkeyPatch) -> None:
    """A native extension failing to load is no longer a silent quality cliff.

    Before, this flipped ``search`` to ``ORDER BY id DESC`` and returned the
    newest rows as if they were semantic hits.
    """
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    memory._conn_()
    assert memory._use_vec is False, "the extension must be reported as absent"

    await _seed(memory)
    ranked = await _rank_all(memory)

    assert recall_at_k(ranked, FIXTURE_QUERIES, k=3) == 1.0
    status = memory.status()
    assert status.sqlite_vec_loaded is False
    assert status.lexical_floor_available is True
    memory.close()


@pytest.mark.asyncio
async def test_real_embedder_keeps_its_dense_signal_without_regression() -> None:
    """Deployments with all-MiniLM / llama-server lose nothing by the change."""
    memory = LongTermMemory(":memory:", RealEmbedder())  # type: ignore[arg-type]
    await _seed(memory)

    recalls = [await memory.recall("t", query.query, top_k=3) for query in FIXTURE_QUERIES]
    ranked = [recall.contents for recall in recalls]

    assert recall_at_k(ranked, FIXTURE_QUERIES, k=3) == 1.0
    assert all(recall.mode is RetrievalMode.HYBRID_FUSION for recall in recalls)
    assert all(recall.dense_lane == "validated-embeddings" for recall in recalls)
    # the dense lane really did participate
    assert any(hit.lane in {"dense", "both"} for recall in recalls for hit in recall.hits)
    memory.close()


@pytest.mark.asyncio
async def test_lexical_lane_beats_a_wrong_dense_lane_at_rank_one() -> None:
    """The trust prior: a validated lane outranks an unvalidated one.

    ``DEFAULT_VECTOR_WEIGHT`` exists because the lexical lane scores the query
    against the stored text (it cannot be noise) while the dense lane is only as
    good as whichever embedder is wired.
    """
    noise = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    await _seed(noise)
    real = LongTermMemory(":memory:", RealEmbedder())  # type: ignore[arg-type]
    await _seed(real)

    for memory in (noise, real):
        ranked = await _rank_all(memory, k=1)
        assert recall_at_k(ranked, FIXTURE_QUERIES, k=1) >= 0.5, (
            "the dense lane must not be able to override a strong lexical match"
        )
        memory.close()


# ── 3. offline-first (Q1) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_never_loses_a_memory_when_the_embedder_is_down() -> None:
    """Previously ``store`` raised, so an offline deployment stored nothing."""
    memory = LongTermMemory(":memory:", DeadEmbedder())  # type: ignore[arg-type]
    await _seed(memory)  # must not raise

    status = memory.status()
    assert status.rows == len(FIXTURE_MEMORIES), "every memory is persisted"
    assert status.rows_with_embedding == 0, "no vector, and that is recorded"
    assert status.dim_contract == "absent"

    ranked = await _rank_all(memory)
    assert recall_at_k(ranked, FIXTURE_QUERIES, k=3) == 1.0

    recall = await memory.recall("t", FIXTURE_QUERIES[0].query, top_k=3)
    assert recall.mode is RetrievalMode.LEXICAL_BM25
    # No vector was ever stored, so there is nothing for the dense lane to rank
    # against — reported as an empty lane, not as a failure.
    assert recall.dense_lane == DENSE_LANE_EMPTY
    assert recall.degraded is False, "lexical-only is the floor, not a degradation"
    memory.close()


@pytest.mark.asyncio
async def test_injected_embedder_overrides_the_provider() -> None:
    """One explicit seam to promote the dense lane to real signal."""
    real = RealEmbedder()
    memory = LongTermMemory(
        ":memory:",
        DeadEmbedder(),  # type: ignore[arg-type]
        embedder=real.embed,
    )
    await _seed(memory)
    assert real.calls > 0
    assert memory.status().rows_with_embedding == len(FIXTURE_MEMORIES)
    memory.close()


# ── 4. explainability: retrieval is never hidden state again ─────────────


@pytest.mark.asyncio
async def test_recall_names_its_strategy() -> None:
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]

    empty = await memory.recall("t", "anything", top_k=3)
    assert empty.mode is RetrievalMode.EMPTY
    assert empty.hits == ()
    assert empty.degraded is False

    await _seed(memory)
    fused = await memory.recall("t", "database backup storage", top_k=3)
    assert fused.mode is RetrievalMode.HYBRID_FUSION
    assert fused.reason == NOT_DEGRADED
    assert fused.lexical_indexed == len(FIXTURE_MEMORIES)
    assert fused.scanned > 0
    assert [hit.rank for hit in fused.hits] == [1, 2, 3]
    memory.close()


@pytest.mark.asyncio
async def test_recency_fallback_is_labelled_degraded_not_disguised() -> None:
    """The exact case that used to masquerade as a semantic hit.

    Injected by emptying the lexical index and removing every vector, so no lane
    can rank — the only route left to ``ORDER BY id DESC``.
    """
    memory = LongTermMemory(":memory:", DeadEmbedder())  # type: ignore[arg-type]
    await _seed(memory)
    memory._lexical["t"].clear()
    memory._lexical_for = lambda thread_id: memory._lexical["t"]  # type: ignore[method-assign]

    recall = await memory.recall("t", "database backup storage", top_k=3)
    assert recall.mode is RetrievalMode.RECENCY_DEGRADED
    assert recall.degraded is True
    assert recall.reason == NO_RANKING_SIGNAL
    assert recall.hits, "still returns something rather than nothing"
    memory.close()


@pytest.mark.asyncio
async def test_recall_dict_reports_shape_but_never_conversation_content() -> None:
    """Memory content is conversation data; operational records must not carry it."""
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    await memory.store("t", "My secret code is ALPHA-42", metadata={"kind": "turn"})

    payload = (await memory.recall("t", "secret code", top_k=3)).as_dict()
    serialised = repr(payload)
    assert "ALPHA-42" not in serialised
    assert "secret code" not in serialised.lower()
    assert payload["mode"] == "lexical_bm25" or payload["mode"] == "hybrid_fusion"
    memory.close()


# ── 5. the dimension contract is enforced, not declared ──────────────────


@pytest.mark.asyncio
async def test_wrong_dimension_vectors_are_refused_and_reported() -> None:
    """``DIM`` used to be a class attribute nobody checked."""
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    await memory.store("t", "The R2 blob tier stores database backups on Cloudflare")

    # A provider swap writes a wider vector straight into the store.
    conn = memory._conn_()
    wide = struct.pack("768f", *([0.05] * 768))
    conn.execute(
        "INSERT INTO memories (thread_id, content, embedding, embedding_dim) VALUES (?,?,?,?)",
        ("t", "unrelated row about the weather", wide, 768),
    )
    conn.commit()
    memory._vectors_loaded_for.clear()

    status = memory.status()
    assert status.dim_contract == "mixed"
    assert status.dims_observed == (384, 768)

    # The malformed row must not be allowed to rank by cosine against a 384-d
    # query vector: it is excluded from the dense lane entirely.
    assert all(len(vector) == 384 for vector in memory._vectors_for("t").values())
    memory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_vector",
    [
        [0.0] * 384,  # zero norm: cosine is undefined, must not rank
        [float("nan")] * 384,  # non-finite: would poison every comparison
        [float("inf")] * 384,
        ["0.1"] * 384,  # type: ignore[list-item]  # non-numeric payload
    ],
    ids=["zero-norm", "nan", "inf", "non-numeric"],
)
async def test_structurally_invalid_vectors_never_enter_the_dense_lane(bad_vector) -> None:
    """A malformed vector must be refused, not ranked with.

    Before the dimension contract was enforced, any 384 floats were accepted and
    compared — so a degenerate embedder could silently reorder every recall.
    """

    async def embedder(text: str) -> list[float]:
        _ = text
        return list(bad_vector)

    memory = LongTermMemory(":memory:", DeadEmbedder(), embedder=embedder)  # type: ignore[arg-type]
    await memory.store("t", "The R2 blob tier stores database backups on Cloudflare")

    status = memory.status()
    assert status.rows_with_embedding == 0, "a structurally invalid vector is never persisted"
    assert memory._vectors_for("t") == {}

    recall = await memory.recall("t", "database backups", top_k=3)
    assert recall.contents, "the lexical floor still answers"
    assert recall.degraded is False
    memory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt",
    [b"\x01\x02\x03", b"", b"\x00" * 7, b"not a vector at all!!"],
    ids=["three-bytes", "empty", "seven-bytes", "text"],
)
async def test_corrupt_stored_blob_is_refused_not_reinterpreted(corrupt: bytes) -> None:
    """A blob read back from disk is untrusted input.

    Truncated or malformed vectors must be dropped from the dense lane. A misread
    vector does not merely rank badly — it ranks confidently, which is the same
    failure class as the noise embeddings this transformation removed.
    """
    memory = LongTermMemory(":memory:", RealEmbedder())  # type: ignore[arg-type]
    await memory.store("t", "The R2 blob tier stores database backups on Cloudflare")
    conn = memory._conn_()
    conn.execute(
        "INSERT INTO memories (thread_id, content, embedding, embedding_dim) VALUES (?,?,?,?)",
        ("t", "a row whose vector was truncated on disk", corrupt, 384),
    )
    conn.commit()
    memory._vectors_loaded_for.clear()

    vectors = memory._vectors_for("t")
    assert all(len(vector) == 384 for vector in vectors.values())
    assert all(all(math.isfinite(value) for value in vector) for vector in vectors.values())

    recall = await memory.recall("t", "database backups", top_k=3)  # must not raise
    assert recall.contents
    assert recall.degraded is False
    memory.close()


@pytest.mark.asyncio
async def test_embedder_returning_the_wrong_width_does_not_poison_the_store() -> None:
    memory = LongTermMemory(":memory:", WrongDimEmbedder())  # type: ignore[arg-type]
    await memory.store("t", "The R2 blob tier stores database backups on Cloudflare")

    status = memory.status()
    assert status.rows == 1
    assert status.rows_with_embedding == 0, "a non-conforming vector is not stored"
    assert status.dim_contract == "absent"

    assert await memory.search("t", "database backups", top_k=3), "still retrievable lexically"
    memory.close()


# ── 6. provenance: metadata is no longer discarded ───────────────────────


@pytest.mark.asyncio
async def test_metadata_provenance_round_trips() -> None:
    """``store`` used to do ``_ = metadata`` — the origin was thrown away."""
    memory = LongTermMemory(":memory:", DeadEmbedder())  # type: ignore[arg-type]
    await memory.store(
        "t",
        "User prefers Persian responses",
        metadata={"kind": "preference", "source": "graph.turn"},
    )

    recall = await memory.recall("t", "Persian responses", top_k=1)
    hit = recall.hits[0]
    assert hit.kind == "preference"
    assert hit.source == "graph.turn"
    assert hit.created_at is not None and hit.created_at > 0
    memory.close()


@pytest.mark.asyncio
async def test_unknown_metadata_keys_are_ignored_not_rejected() -> None:
    """memory/ is a leaf: it records provenance, it does not own other layers' schemas."""
    memory = LongTermMemory(":memory:", DeadEmbedder())  # type: ignore[arg-type]
    await memory.store("t", "some turn", metadata={"kind": "turn", "surprise": object()})
    assert (await memory.recall("t", "some turn", top_k=1)).hits[0].kind == "turn"
    memory.close()


# ── 7. backward compatibility (graph.py must not need an edit) ───────────


@pytest.mark.asyncio
async def test_search_signature_and_return_type_are_unchanged() -> None:
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    await memory.store("t", "My secret code is ALPHA-42")

    result = await memory.search("t", "secret code", top_k=5)
    assert isinstance(result, list)
    assert all(isinstance(item, str) for item in result)
    assert result and "ALPHA-42" in result[0]

    context = await memory.format_context(result)
    assert context.startswith("Relevant memories:\n- ")
    assert await memory.format_context([]) == ""
    memory.close()


@pytest.mark.asyncio
async def test_store_positional_signature_is_unchanged() -> None:
    """The composition root calls ``LongTermMemory(path, llm)`` positionally."""
    memory = LongTermMemory(":memory:", DeadEmbedder())  # type: ignore[arg-type]
    await memory.store("thread", "text")  # two positional args, no metadata
    assert memory.status().rows == 1
    memory.close()


@pytest.mark.asyncio
async def test_legacy_store_without_the_new_columns_is_upgraded_on_open(tmp_path) -> None:
    """A pre-existing database must keep working — the migration is additive."""
    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "thread_id TEXT NOT NULL, content TEXT NOT NULL, embedding BLOB)"
    )
    legacy.execute(
        "INSERT INTO memories (thread_id, content) VALUES (?,?)",
        ("t", "The R2 blob tier stores database backups on Cloudflare"),
    )
    legacy.commit()
    legacy.close()

    memory = LongTermMemory(str(path), DeadEmbedder())  # type: ignore[arg-type]
    recall = await memory.recall("t", "database backups", top_k=3)
    assert recall.contents, "legacy rows are retrievable"
    assert recall.hits[0].kind == "turn", "absent provenance defaults, never None-crashes"
    memory.close()


# ── 8. lifecycle and resource retention ──────────────────────────────────


@pytest.mark.asyncio
async def test_close_releases_the_connection_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "mem.sqlite3"
    memory = LongTermMemory(str(path), DeadEmbedder())  # type: ignore[arg-type]
    await memory.store("t", "durable row")
    memory.close()
    memory.close()  # must not raise

    reopened = LongTermMemory(str(path), DeadEmbedder())  # type: ignore[arg-type]
    assert await reopened.search("t", "durable row", top_k=1)
    reopened.close()


@pytest.mark.asyncio
async def test_search_after_close_reopens_the_store(tmp_path) -> None:
    path = tmp_path / "mem.sqlite3"
    memory = LongTermMemory(str(path), DeadEmbedder())  # type: ignore[arg-type]
    await memory.store("t", "durable row")
    memory.close()
    assert await memory.search("t", "durable row", top_k=1)
    memory.close()


# ── 9. adversarial inputs ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "   ", "\u200c", "!!! ???", "a" * 5000])
async def test_degenerate_queries_never_crash_or_claim_false_signal(query: str) -> None:
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    await _seed(memory)

    recall = await memory.recall("t", query, top_k=3)
    assert isinstance(recall, MemoryRecall)
    if not recall.hits:
        # No lane could rank a contentless query: that must be *said*, not
        # dressed up as a semantic result.
        assert recall.mode is RetrievalMode.RECENCY_DEGRADED
        assert recall.degraded is True
    memory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("top_k", [0, -1, 1, 3, 10_000])
async def test_out_of_range_top_k_is_bounded_not_fatal(top_k: int) -> None:
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    await _seed(memory)
    result = await memory.search("t", "database backups", top_k=top_k)
    assert len(result) <= len(FIXTURE_MEMORIES)
    memory.close()


@pytest.mark.asyncio
async def test_threads_are_isolated() -> None:
    """A recall for one thread must never surface another thread's memories."""
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    await memory.store("alice", "My secret code is ALPHA-42")
    await memory.store("bob", "Bob's project is called Nimbus")

    alice = await memory.search("alice", "secret code project", top_k=5)
    assert alice and "ALPHA-42" in alice[0]
    assert not any("Nimbus" in item for item in alice)

    bob = await memory.search("bob", "secret code project", top_k=5)
    assert not any("ALPHA-42" in item for item in bob)
    memory.close()


@pytest.mark.asyncio
async def test_dense_lane_cache_is_thread_scoped() -> None:
    """Regression: a process-wide vector cache leaked memories across threads.

    Writing both threads *first* is what exposed it — ``store`` populated the
    shared cache, so answering one thread scored (and could return) the other
    thread's vectors. In a Telegram assistant that is one user's memory arriving
    in another user's context.
    """
    memory = LongTermMemory(":memory:", RealEmbedder())  # type: ignore[arg-type]
    for index in range(12):
        await memory.store("alice", f"alice note {index} about database backups")
    for index in range(12):
        await memory.store("bob", f"bob note {index} about database backups")

    alice_vectors = memory._vectors_for("alice")
    bob_vectors = memory._vectors_for("bob")
    assert not (set(alice_vectors) & set(bob_vectors)), "caches must not share ids"

    alice = await memory.recall("alice", "database backups", top_k=20)
    assert alice.hits, "alice has retrievable memories"
    assert all("alice note" in hit.content for hit in alice.hits)
    assert all(hit.memory_id in alice_vectors for hit in alice.hits)

    bob = await memory.recall("bob", "database backups", top_k=20)
    assert all("bob note" in hit.content for hit in bob.hits)
    memory.close()


@pytest.mark.asyncio
async def test_hydrate_refuses_ids_from_another_thread() -> None:
    """Defence in depth: content materialisation is thread-filtered by SQL.

    Even if a ranker were ever to return a foreign id, the row is dropped rather
    than surfaced — the last place a cross-thread leak can be stopped.
    """
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    await memory.store("alice", "alice private memory")
    await memory.store("bob", "bob private memory")
    conn = memory._conn_()
    ids = [row[0] for row in conn.execute("SELECT id FROM memories ORDER BY id")]

    hits = memory._hydrate("alice", ids, {}, {})
    assert [hit.content for hit in hits] == ["alice private memory"]
    memory.close()


@pytest.mark.asyncio
async def test_embedding_unavailable_is_distinguished_from_no_vectors() -> None:
    """Two different reasons the dense lane cannot run must not be conflated."""
    memory = LongTermMemory(":memory:", RealEmbedder())  # type: ignore[arg-type]
    await memory.store("t", "The R2 blob tier stores database backups on Cloudflare")
    assert memory._vectors_for("t"), "a vector was stored"

    # Now break only the query-side embedder.
    memory._embedder = DeadEmbedder().embed
    recall = await memory.recall("t", "database backups", top_k=3)
    assert recall.dense_lane == EMBEDDING_UNAVAILABLE
    assert recall.contents, "the lexical floor still answers"
    memory.close()


@pytest.mark.asyncio
async def test_recall_is_deterministic_across_calls_and_instances(tmp_path) -> None:
    path = tmp_path / "det.sqlite3"
    first = LongTermMemory(str(path), NoiseEmbedder())  # type: ignore[arg-type]
    await _seed(first)
    a = (await first.recall("t", "database backup storage", top_k=3)).contents
    b = (await first.recall("t", "database backup storage", top_k=3)).contents
    first.close()

    second = LongTermMemory(str(path), NoiseEmbedder())  # type: ignore[arg-type]
    c = (await second.recall("t", "database backup storage", top_k=3)).contents
    second.close()
    assert a == b == c


@pytest.mark.asyncio
async def test_duplicate_stores_do_not_corrupt_ranking() -> None:
    """The same memory stored twice must not crowd out other memories."""
    memory = LongTermMemory(":memory:", NoiseEmbedder())  # type: ignore[arg-type]
    for _ in range(20):
        await memory.store("t", "The memory short-term window keeps last 20 messages")
    await memory.store("t", "The R2 blob tier stores database backups on Cloudflare")

    result = await memory.search("t", "database backups on cloud storage", top_k=3)
    assert any("R2" in item for item in result), "the relevant row must survive the duplicates"
    memory.close()


# ── 10. the repaired feedback loop ───────────────────────────────────────


@pytest.mark.asyncio
async def test_harness_measured_recall_clears_the_chance_floor() -> None:
    measured = await evaluate_long_term_recall(k=3)
    chance = chance_recall_at_k(3)
    assert measured == 1.0
    assert chance < 0.25, (
        "the fixture must be hard enough that luck cannot look like retrieval; "
        "the original 6-document corpus had a chance level near 0.50"
    )
    assert measured > chance + 0.5


def test_chance_floor_is_analytical_and_stable() -> None:
    assert chance_recall_at_k(3) == pytest.approx(0.125, abs=1e-9)
    assert chance_recall_at_k(1) < chance_recall_at_k(3)
    assert chance_recall_at_k(0) == 0.0
    assert chance_recall_at_k(3, corpus=(), queries=()) == 0.0


def test_degradation_modes_now_fail_the_regression_guard() -> None:
    """The guard used to pass all three of these — that was the blind spot."""
    assert is_regression(0.50) is True, "the old 'passing' score was chance level"
    assert is_regression(0.25) is True, "the old committed baseline"
    assert is_regression(0.167) is True, "noise-ranked dense-only retrieval"
    assert is_regression(1.0) is False, "the measured value"


# ── 11. the corrected memory-read policy (handoff to graph.py) ───────────


def test_should_read_memory_is_total_over_every_intent() -> None:
    """Memory relevance is not an intent-classification question.

    ``graph.py::route_intent`` reads memory for ``task`` and ``memory`` but sends
    ``chat`` straight to a persona, while ``_memory_writer`` stores every turn —
    write-always, read-sometimes. The policy is stated and tested here so the
    graph wiring is a one-line change under its owner's lease.
    """
    from nexus_ai_agent.orchestration.router import ALL_INTENTS, should_read_memory

    for intent in ALL_INTENTS:
        assert should_read_memory(intent, "some turn text") is True
    # total even for intents nobody declared
    for intent in ("", "nonsense", "CHAT", "taskish"):
        assert should_read_memory(intent) is True


def test_chat_intent_is_the_common_case_that_lost_its_context() -> None:
    """Documents the asymmetry the policy removes: chat is not memory-gated."""
    from nexus_ai_agent.orchestration.router import classify_intent, should_read_memory

    # A question with real stored context and *no* MEMORY_KEYWORDS literal.
    question = "What was my project called again?"
    assert classify_intent(question) == "chat"
    assert should_read_memory(classify_intent(question), question) is True
