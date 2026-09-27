"""Generic semantic reference ownership and provenance validation.

Vision (and future packs) use typed semantic references — e.g. ``MaskRef(mask_id,
source_asset_id)``, ``FaceTrackSet(track_set_id, source_asset_id)`` — to name
derived artefacts. The architecture must guarantee that a caller cannot fabricate
a semantically impossible reference that passes validation:

* fake track ID that was never produced
* wrong asset type (mask supplied where a track is expected)
* reference to an unrelated source asset (cross-contamination)
* stale derived reference whose producer ancestry does not match the declared source
* reference that exists syntactically but was not produced by the declared producer

This module provides the *generic* enforcement. Vision (or any pack) supplies a
small mapping ``field -> expected_result_kind``; the validator checks ownership,
existence and kind *without* understanding pack-specific semantics. The helper
is intentionally placed in the core ``creative.studio`` package so it scales
beyond Vision.

Rules (fail-closed):

1. Any dict containing ``source_asset_id`` must have ``source_asset_id == source_id``
   (the operation's source clip). This prevents cross-source contamination.
2. The reference's own identifier (``mask_id``, ``track_set_id``, ``track_id`` …)
   must not collide with the source asset id.
3. If an ``AssetRecord`` with that identifier *exists* in the project, its
   ``provenance.source_asset_id`` (if present) and its ``provenance.result_kind``
   (if present) must match the caller's claim. A mismatch means a stale or
   wrong-type reference.
4. Where the caller supplies a concrete expectation (``field -> result_kind``),
   a mismatch is an error even when the referenced asset does not yet exist?
   No — for planning-only substrates a reference may be *intent* (synthetic ID)
   before any executor has materialised it. In that case we only enforce (1)
   and (2); the existence check is *advisory* and only fires when the asset
   actually exists. This keeps the validator generic while still catching
   cross-contamination and type-confusion when ancestry is present.
5. Sky/auxiliary asset ids (bare ``sky_asset_id`` string) are validated for
   existence separately by the caller.

The validator is pure and leaves no state; it raises ``CommandValidationError``
on the first violation.
"""
from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.studio.models import AssetRecord, CommandValidationError

# Known identifier keys that may appear inside a semantic reference dict.
# Ordered most-specific first; the first match (excluding source_asset_id) is taken
# as the reference's own id.
_CANDIDATE_ID_KEYS = (
    "mask_id",
    "track_set_id",
    "landmark_set_id",
    "track_id",
    "boundary_set_id",
    "moment_set_id",
    "curve_id",
    "layer_id",
    "asset_id",
)


def _reference_id(value: dict[str, Any]) -> str | None:
    for key in _CANDIDATE_ID_KEYS:
        if key in value:
            v = value[key]
            if isinstance(v, str):
                return v
    # Fallback: any *_id other than source_asset_id
    for k, v in value.items():
        if k != "source_asset_id" and k.endswith("_id") and isinstance(v, str):
            return v
    return None


def _collect_semantic_dicts(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Collect top-level semantic ref dicts and nested ones (e.g. FaceLandmarkSet.face_tracks)."""
    collected: list[tuple[str, dict[str, Any]]] = []

    def walk(prefix: str, obj: Any) -> None:
        if isinstance(obj, dict):
            if "source_asset_id" in obj:
                collected.append((prefix, obj))
            # Recurse into nested dict values (e.g. face_tracks inside FaceLandmarkSet)
            for k, v in obj.items():
                if isinstance(v, dict):
                    walk(f"{prefix}.{k}" if prefix else k, v)
                elif isinstance(v, list):
                    for i, item in enumerate(v):
                        if isinstance(item, dict):
                            walk(f"{prefix}.{k}[{i}]", item)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if isinstance(item, dict):
                    walk(f"{prefix}[{i}]", item)

    for field, value in data.items():
        if isinstance(value, dict) and "source_asset_id" in value:
            collected.append((field, value))
            # also walk nested inside
            for k, v in value.items():
                if isinstance(v, dict) and "source_asset_id" in v:
                    walk(f"{field}.{k}", v)
        elif isinstance(value, dict):
            walk(field, value)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    walk(f"{field}[{i}]", item)
    # Deduplicate by identity
    seen: set[int] = set()
    uniq: list[tuple[str, dict[str, Any]]] = []
    for field, d in collected:
        iid = id(d)
        if iid not in seen:
            seen.add(iid)
            uniq.append((field, d))
    return uniq


def validate_semantic_references(
    data: dict[str, Any],
    source_id: str,
    assets: dict[str, AssetRecord],
    *,
    operation: str,
    expected_kinds: dict[str, str] | None = None,
) -> None:
    """
    Validate every semantic reference in ``data`` against ``source_id`` and ``assets``.

    ``expected_kinds`` maps field name (top-level key of ``data``) to the
    ``result_kind`` the referenced asset must have when it exists. Unknown fields
    are not kind-checked (only ownership).
    """
    expected_kinds = expected_kinds or {}
    for field, ref in _collect_semantic_dicts(data):
        src = ref.get("source_asset_id")
        if not isinstance(src, str):
            raise CommandValidationError(f"{operation}: {field} missing source_asset_id")
        if src != source_id:
            raise CommandValidationError(f"{operation}: {field} belongs to another source asset")
        ref_id = _reference_id(ref)
        if ref_id is None:
            # No identifier found — the model should have rejected this, but fail closed.
            raise CommandValidationError(f"{operation}: {field} missing reference identifier")
        if ref_id == source_id:
            raise CommandValidationError(f"{operation}: {field} reference collides with source asset id")
        # If an asset with this id exists, its provenance must be consistent.
        asset = assets.get(ref_id)
        if asset is not None:
            prov = asset.provenance or {}
            prov_source = prov.get("source_asset_id")
            if isinstance(prov_source, str) and prov_source != source_id:
                raise CommandValidationError(
                    f"{operation}: {field} reference {ref_id!r} was derived from another source {prov_source!r}"
                )
            # Check parent ancestry as well (some assets may not have provenance)
            if asset.parent_asset_ids and source_id not in asset.parent_asset_ids:
                # For single-parent derived assets, parent must be source; for multi-parent, must include source
                # Allow case where asset is source itself (parent empty) already excluded
                raise CommandValidationError(
                    f"{operation}: {field} reference {ref_id!r} has ancestry {asset.parent_asset_ids!r} not matching source {source_id!r}"
                )
            expected = expected_kinds.get(field.split(".")[0])  # top-level field name
            if expected is not None:
                # Only check when the asset actually declares a result_kind
                prov_kind = prov.get("result_kind")
                if isinstance(prov_kind, str) and prov_kind != expected:
                    raise CommandValidationError(
                        f"{operation}: {field} expects {expected!r} but asset {ref_id!r} is {prov_kind!r}"
                    )
                # Also allow derived assets whose result_kind is stored elsewhere? Fallback to check asset's provenance result_kind strictly.
                # If asset has no provenance kind, don't fail — it may be a source asset reused as reference (e.g. sky image).
                # So only fail when prov_kind is present and mismatched.
__all__ = ["validate_semantic_references"]
