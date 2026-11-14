"""Recipe identity: the question "is this the artifact I would have produced?".

The engine reuses a previously published artifact when a request is replayed.
That reuse is only honest if the *recipe* is the same in every way that can
change the bytes.  The first implementation keyed the decision on the output
asset id alone, so ``denoise(strength=0.9)`` on an id previously produced with
``strength=0.2`` returned the older file and reported ``reused_existing=True``
— evidence that described a render which never happened.

:func:`recipe_identity` is the one place that answers the question.  It hashes
the whole tuple that determines the output:

* ``operation`` — the named transformation;
* ``source_sha256`` — the exact input bytes (never the asset id);
* ``parameters`` — every parameter the builder reads;
* ``profiles`` — the canonical stream shape and the loudness target;
* ``engine`` — the backend identity (binary name + the measurement/decode
  strategies), so an engine upgrade cannot silently reuse an old artifact.

The encoding is canonical JSON (sorted keys, no whitespace) so two equal recipes
built in a different key order hash identically.  ``None`` and a missing key are
deliberately different: a recipe that *pins* a loudness target is not the same
recipe as one that does not, even when the pinned value happens to be ``None``
at a different level of the structure.
"""

from __future__ import annotations

import hashlib
from typing import Any

from nexus_ai_agent.creative.audio_engine.models import canonical_json

#: Bumped whenever the meaning of a component changes.  It is part of the hash,
#: so raising it invalidates every cached artifact instead of reusing one whose
#: identity was computed under different rules.
RECIPE_IDENTITY_VERSION = 2


def recipe_identity(
    *,
    operation: str,
    source_sha256: str,
    parameters: dict[str, Any],
    profiles: dict[str, Any],
    engine: dict[str, Any],
) -> str:
    """Return the stable identity of the recipe that would produce the artifact.

    Raises :class:`~nexus_ai_agent.creative.audio_engine.models.MediaContractError`
    through :func:`canonical_json` when a parameter is not canonically
    serialisable — an unhashable recipe is a contract error, not an empty hash.
    """
    payload = {
        "recipe_identity_version": RECIPE_IDENTITY_VERSION,
        "operation": operation,
        "source_sha256": source_sha256,
        "parameters": parameters,
        "profiles": profiles,
        "engine": engine,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"recipe:{digest}"


__all__ = ["RECIPE_IDENTITY_VERSION", "recipe_identity"]
