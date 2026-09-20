"""Nagar capability packs — Wave 2a: the pack substrate.

A pack is **data plus pre-registered adapters**, never arbitrary code:

* :mod:`nexus_ai_agent.creative.packs.manifest` — the strict, immutable
  ``nexus.capability-pack.v1`` manifest model (TDD section 2.5);
* :mod:`nexus_ai_agent.creative.packs.verify` — structural, policy and
  runtime allow-list verification;
* :mod:`nexus_ai_agent.creative.packs.registry` — registration and
  activation over the studio's ``CapabilityRegistry``.

Wave 2a ships the substrate and the eighth pack's manifest
(``nexus.slideshow.compose``).  The pack's operations are registered in
Wave 2b; until then they are reported as *pending* and the pack cannot be
activated.
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.manifest import (
    MANIFEST_SCHEMA,
    PROTOCOL_VERSION,
    STATE_SCHEMA,
    ArtifactSpec,
    CapabilityPackManifest,
    PackManifestError,
    dump_manifest,
    load_manifest,
    parse_semver,
)
from nexus_ai_agent.creative.packs.registry import (
    BUILTIN_MANIFEST_GLOB,
    PackRegistry,
    PackRegistryError,
    RegisteredPack,
)
from nexus_ai_agent.creative.packs.verify import (
    MEDIA_EGRESS_PERMISSION,
    VerificationIssue,
    VerificationReport,
    verify_manifest,
    verify_manifest_file,
)

__all__ = [
    "BUILTIN_MANIFEST_GLOB",
    "MANIFEST_SCHEMA",
    "MEDIA_EGRESS_PERMISSION",
    "PROTOCOL_VERSION",
    "STATE_SCHEMA",
    "ArtifactSpec",
    "CapabilityPackManifest",
    "PackManifestError",
    "PackRegistry",
    "PackRegistryError",
    "RegisteredPack",
    "VerificationIssue",
    "VerificationReport",
    "dump_manifest",
    "load_manifest",
    "parse_semver",
    "verify_manifest",
    "verify_manifest_file",
]
