"""Content-addressed identity: the property that makes the rest of the plane possible.

Every identity in the Creative IR is the SHA-256 of the element's own *semantic
payload* joined with the identities of the elements it refers to. The document
is therefore a Merkle tree, and four consequences follow -- each one is a
capability a later slice of this plane needs and cannot get any other way:

1. **Deterministic compilation.** Building the same creative document twice
   yields byte-identical ids. There is no ``uuid4`` anywhere in the identity
   path, so recompiling an unchanged intent cannot churn ids, cannot invalidate
   downstream caches, and cannot make two agents produce different plans for
   the same input.
2. **Cheap semantic diff.** Because a parent's id commits its children's ids,
   two documents that differ in one scene share every id outside that scene's
   subtree. "What changed between v1 and v2" is a tree walk, not a text diff.
3. **Cheap semantic revision.** A revision that rewrites one scene re-hashes
   only that subtree; every untouched element keeps its identity, so a revision
   is an *edit*, not a rebuild.
4. **Self-verification.** :func:`verify_identity` re-derives every id from
   content. A hand-edited or stale document is caught structurally, before it
   reaches a compiler.

Canonical form
--------------
Hashing needs one byte string per document, so the payload is serialised with
sorted keys, minimal separators and ``ensure_ascii=False``. The IR itself
carries **no floats**: time is integer microseconds, normalised quantities are
integer per-mille (0..1000) and signed continuous parameters are integer
milli-units. That is a deliberate constraint, not a style choice -- IEEE-754
round-tripping and repr drift are the two easiest ways to make a "deterministic"
hash silently platform-dependent.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from nexus_ai_agent.creative.intelligence.errors import SerializationError

#: IR document schema identifier, hashed into the root identity so a v2
#: document can never be mistaken for a v1 one. Typing it as a ``Literal`` (not
#: ``str``) is what lets ``CreativeWork.ir_version`` stay a closed set while
#: still defaulting to this constant.
IRVersion = Literal["nexus.creative-ir.v1"]
IR_VERSION: IRVersion = "nexus.creative-ir.v1"

#: Length of the hex digest carried in an identity. 20 hex chars is 80 bits:
#: for any document this plane will ever hold (thousands of nodes) the birthday
#: collision probability is below 1e-18, while staying readable in a log line.
DIGEST_HEX_LENGTH = 20

#: Identity prefixes. Kept short and stable -- they appear in every id the
#: plane ever emits, so they are part of the wire contract, not cosmetics.
PREFIX_WORK = "wrk"
PREFIX_ASSET = "ast"
PREFIX_SCENE = "scn"
PREFIX_LAYER = "lay"
PREFIX_EFFECT = "efx"
PREFIX_TRANSITION = "trn"
PREFIX_CONSTRAINT = "cst"
PREFIX_TRACK = "trk"

#: Time base shared with the rest of the creative stack. ``studio/models.py``
#: declares the same constant; the IR redeclares it instead of importing it
#: because the plane must not depend on the execution substrate (see
#: ``tests/architecture/test_creative_intelligence_boundary.py``). A contract
#: test pins the two together so a change on either side is loud.
MICROSECONDS_PER_SECOND = 1_000_000


def canonical_json(payload: Any) -> str:
    """Serialise a payload to the one byte string that gets hashed.

    Sorted keys at every depth and minimal separators make the output
    independent of construction order; ``ensure_ascii=False`` keeps a Persian
    brief from being escaped into a different (but equally valid) byte string
    than the same brief written in ASCII-escaped form.
    """
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - guarded by _json_default
        raise SerializationError(f"payload is not canonically serialisable: {exc}") from exc


def _json_default(value: Any) -> Any:
    """Fail closed on anything that is not already JSON-native.

    Silently ``str()``-ing an unknown object would produce a *plausible* hash
    for a type nobody intended to hash. Raising keeps the identity contract
    honest: if a new field type enters the IR, this forces a decision about how
    it participates in identity.
    """
    raise SerializationError(
        f"cannot canonically serialise {type(value).__name__}; "
        "the Creative IR identity payload must be JSON-native"
    )


def content_id(prefix: str, payload: Any) -> str:
    """Return the content address ``<prefix>_<digest>`` of ``payload``.

    The prefix is *not* part of the hashed material -- it is a readability
    marker -- but the digest is over the payload alone, so the same content
    under two prefixes yields the same suffix. That is intentional: it makes a
    mistyped prefix obvious in a diff instead of producing two unrelated ids.
    """
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:DIGEST_HEX_LENGTH]}"


def sealed_id(
    prefix: str, payload: Any, children: tuple[str, ...], *, preserve_order: bool = False
) -> str:
    """Content address of an element *plus* the identities it commits to.

    ``children`` must already be sealed identities. Including them is what makes
    the document a Merkle tree rather than a bag of independent hashes: a change
    anywhere below an element changes that element's identity, and therefore
    every identity above it, all the way to the root.

    Most collections in the IR are semantic *sets*: assets, constraints and
    transitions keep their identity regardless of the order a caller happened to
    enumerate them in, so their children are normalized by sorting. A few are
    semantic *sequences*: layer stacking inside a scene, effect ordering inside a
    layer, and layer ordering on a render track. Those call sites pass
    ``preserve_order=True`` so a re-ordered sequence changes identity.
    """
    material = {
        "payload": payload,
        "children": list(children) if preserve_order else sorted(children),
    }
    return content_id(prefix, material)
