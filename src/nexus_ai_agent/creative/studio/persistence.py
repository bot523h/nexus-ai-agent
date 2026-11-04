"""Canonical byte form of a Studio ``Project`` state, and the durable port.

The Studio core stays storage-free (Wave 1 isolation): this module only defines
the deterministic serialization a durable store persists and rebuilds from.  A
revision's identity is the content address of these bytes, so serialization
must be canonical (sorted keys, no insignificant whitespace) or two equal
states would hash differently and the store could fork history.

The durable store itself lives outside this package and satisfies the
``ProjectRevisionStore`` port declared alongside the domain types; the Studio
package never imports storage.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from nexus_ai_agent.creative.studio.models import Project


def canonical_state_bytes(project: Project) -> bytes:
    """Deterministic UTF-8 encoding of a project state.

    ``state_revision`` is included on purpose: the durable snapshot must let a
    fresh process rebuild the *exact* revision (counter and content), while the
    content address is still stable for an identical state.
    """
    payload: dict[str, Any] = project.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def content_address(state_bytes: bytes) -> str:
    """The revision id of a canonical state: ``sha256:<hex>`` over its bytes."""
    return "sha256:" + hashlib.sha256(state_bytes).hexdigest()


def deserialize_project(raw: str | bytes) -> Project:
    """Rebuild a validated ``Project`` from its canonical byte form."""
    data = json.loads(raw)
    return Project.model_validate(data)
