"""Canonical construction of the Asset Library service.

The library is deliberately provider- and framework-free, which means it has no
business deciding where its blobs live or which authorizer it trusts.  This
module is the single reviewed composition site that answers both questions from
server-controlled configuration, so no caller (and no request payload) can point
the library at an attacker-chosen directory or mint its own grant.

* the blob root is a fixed subdirectory of ``settings.creative_temp_dir`` — the
  same persistent workspace the rest of the creative lane already uses, never a
  path a caller supplies;
* the authorizer is the existing Studio ``SQLApplicationDirectory`` seam wrapped
  in :class:`StudioAuthorizationSeam`, so an ingest is authorized by the same
  durable membership the CommandBus already consults.

Nothing here is a second authority: it wires the one blob store, the one
authorizer and the one database session factory the library already declares.
"""

from __future__ import annotations

from pathlib import Path

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.creative.assets.authz import StudioAuthorizationSeam
from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.service import AssetLibraryService

#: Subdirectory of ``settings.creative_temp_dir`` holding the content-addressed
#: blobs.  Fixed so the location is a deployment decision, not a request input.
BLOB_DIRNAME = "asset_library"


def asset_blob_root() -> Path:
    """The canonical, server-controlled blob root for the Asset Library."""
    return Path(get_settings().creative_temp_dir) / BLOB_DIRNAME


def build_asset_library() -> AssetLibraryService:
    """Build the canonical Asset Library bound to the app's blob root + authority.

    The authorizer import is deferred so importing this module never drags the
    application-directory adapter (and its SQL engine) into a caller that only
    wants the blob root.
    """
    from nexus_ai_agent.adapters.sql_application_directory import SQLApplicationDirectory

    return AssetLibraryService(
        blob_store=LocalContentAddressedBlobStore(asset_blob_root()),
        authorizer=StudioAuthorizationSeam(SQLApplicationDirectory()),
    )


__all__ = ["BLOB_DIRNAME", "asset_blob_root", "build_asset_library"]
