"""The ``nexus.media.transform`` capability pack.

This pack closes the gap the composition runtime found in the live registry:
NEXUS had **no** canonical capability for a still-image resize, a grayscale
conversion or a still-image-to-video encode.  The abandoned prototype
(``trae/agent-CCO4Fr``) solved that by shipping a *private* capability
dictionary — a parallel registry, which is exactly what this repository's
single-authority law forbids.

The lawful way to add a capability is the one the pack substrate already
provides: a manifest that *declares* the operations, and a registrar that
registers them on the **canonical** :class:`CapabilityRegistry` so that
``build_runtime_registry()`` — the one registry the command bus authorizes
against — knows them.

The pack itself is **pure** (stdlib + pydantic, no subprocess, no filesystem,
no network — enforced by ``tests/architecture/test_pack_manifest_is_data_only.py``).
The three operations here *record* a derived asset in the project state; the
media itself is produced by the canonical FFmpeg adapter in
``nexus_ai_agent.creative.slideshow.ffmpeg`` and by the composition layer's
engine, which is the same split the slideshow pack uses
(``packs/slideshow/operations.py`` records, ``creative/slideshow/upscale.py``
realises).
"""

from __future__ import annotations

__all__ = ["DOMAIN", "PACKAGE_ID", "PACK_VERSION"]

#: The canonical package id.  It must be a segment of every capability id the
#: manifest declares (``nexus.media.transform`` -> domain ``media``).
PACKAGE_ID = "nexus.media.transform"

#: The registry domain the pack's operations live in.
DOMAIN = "media"

#: The pack version.  It is the *pack* version; the capability version is
#: declared per operation by the registry (``capability_version``).
PACK_VERSION = "1.0.0"
