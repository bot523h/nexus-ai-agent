"""Deterministic, offline text embeddings (no model, no network, no weights).

The default embedding provider for the knowledge index is a
**hashed bag-of-tokens** vector: unicode-folded tokens (the same folding the
existing RAG core uses) are hashed into a fixed number of buckets and
L2-normalised.  Properties that matter for this subsystem:

* **Deterministic** — same text ⇒ same vector, on every machine, forever;
  no model download, no runtime weights, no external service.
* **Meaningful ranking** — cosine over hashed token counts behaves like a
  lexical similarity, so retrieval over fixtures is genuinely tested rather
  than mocked.
* **Pure stdlib** — importable without ``llama_index`` or
  ``sentence-transformers``; unit-testable in milliseconds.

Operators who want semantic vectors configure
``NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER=sentence-transformers`` with an
explicitly downloaded local model directory (see
``docs/architecture/KNOWLEDGE_RETRIEVAL.md`` §4) — the subsystem never
downloads a model or falls back to a cloud provider on its own.
"""

from __future__ import annotations

import hashlib
import math

from nexus_ai_agent.features.rag_core import tokenize

__all__ = ["EMBEDDING_DIM", "hash_embed"]

#: Bucket count for the hashed bag-of-tokens representation.
EMBEDDING_DIM = 256


def hash_embed(texts: list[str], *, dim: int = EMBEDDING_DIM) -> list[list[float]]:
    """Embed each text as a deterministic hashed bag-of-tokens vector.

    Token weights are ``1 + log(tf)`` damping so repetition helps less than
    distinct terms; the vector is L2-normalised (all-zero maps to all-zero).
    """
    if dim < 1:
        raise ValueError("dim must be >= 1")
    vectors: list[list[float]] = []
    for text in texts:
        vector = [0.0] * dim
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        dampened = [
            math.copysign(1.0 + math.log(abs(value)), value) if value else 0.0 for value in vector
        ]
        norm = math.sqrt(sum(value * value for value in dampened))
        if norm > 0.0:
            dampened = [value / norm for value in dampened]
        vectors.append(dampened)
    return vectors
