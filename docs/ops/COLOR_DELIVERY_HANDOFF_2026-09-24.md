# Handoff: color.3 + `delivery.export_otio` delegation (BLOCKED_SHARED_CONTRACT)

Date: 2026-09-24 · Author: Creative Systems Engineer, session 2 (`task-176`,
branch `arena/01a0d2a2-nexus-ai-agent`) · Owner after unblock: whoever lands
PR#33 first, then any free creative agent.

## 1. Status (verified 2026-09-24, not assumed)

* PR#33 is **OPEN** and **CONFLICTING** and its file list still contains
  `src/nexus_ai_agent/creative/packs/delivery/models.py` and
  `src/nexus_ai_agent/creative/packs/delivery/operations.py`.
* Verdict: **BLOCKED_SHARED_CONTRACT** — this session did not touch either
  file (prove: `git diff main...HEAD --stat` shows no delivery file). The
  manifest (`pack.manifest.json`) is *not* in PR#33's list, but changing its
  capabilities without the matching handlers would break the activation gate,
  so it is intentionally left untouched as well.
* What is ready below: exact patches for `color.white_balance`,
  `color.hdr_tonemap`, `color.deband_denoise` (TDD §1.8, all Level B),
  the one-call-site `delivery.export_otio → creative/otio` delegation, the
  manifest delta, the test delta, and integration notes. Apply in order
  after PR#33 merges (or rebases away from these files).

## 2. Patch A — `delivery/models.py`: three typed inputs + ids

Follows the file's existing contract (frozen, `extra="forbid"`, bounded
numerics, optional `output_asset_id`):

```python
OPERATION_WHITE_BALANCE = "color.white_balance"
OPERATION_HDR_TONEMAP = "color.hdr_tonemap"
OPERATION_DEBAND_DENOISE = "color.deband_denoise"


class WhiteBalanceInput(BaseModel):
    """Input payload for ``color.white_balance`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    temperature_k: int = Field(
        default=6500, ge=2000, le=12000, description="White-balance target in Kelvin."
    )
    tint: float = Field(default=0.0, ge=-50.0, le=50.0, description="Green-magenta tint.")
    mode: Literal["manual", "auto"] = "manual"
    output_asset_id: str | None = None


class HdrTonemapInput(BaseModel):
    """Input payload for ``color.hdr_tonemap`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    source_space: Literal["bt2020", "acescc", "pq", "hlg"] = "bt2020"
    target_space: Literal["bt709", "srgb"] = "bt709"
    exposure_ev: float = Field(default=0.0, ge=-4.0, le=4.0)
    output_asset_id: str | None = None


class DebandDenoiseInput(BaseModel):
    """Input payload for ``color.deband_denoise`` (Level B)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_asset_id: str = Field(min_length=1)
    quality_profile: Literal["fast", "balanced", "high"] = "balanced"
    temporal_radius: int = Field(default=1, ge=0, le=4)
    output_asset_id: str | None = None
```

## 3. Patch B — `delivery/operations.py`: three pure handlers + registration

Same recipe as `_apply_lut` (validate → require video → digest-seed derived
`AssetRecord` → outcome). Deterministic default output ids
(`{clip}_whitebalance`, `{clip}_tonemap`, `{clip}_deband`):

```python
def _white_balance(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for color.white_balance."""
    payload = WhiteBalanceInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"color.white_balance references unknown clip asset: {payload.clip_asset_id!r}"
        )
    clip_rec = known[payload.clip_asset_id]
    if clip_rec.media_kind != "video":
        raise CommandValidationError(
            f"color.white_balance requires video asset, got: {clip_rec.media_kind!r}"
        )
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_whitebalance"
    digest_seed = (
        f"{clip_rec.content_sha256}:whitebalance:{payload.temperature_k}:"
        f"{payload.tint}:{payload.mode}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()
    graded_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=clip_rec.duration_us,
        parent_asset_ids=(clip_rec.asset_id,),
        provenance={
            "source_asset_id": clip_rec.asset_id,
            "temperature_k": payload.temperature_k,
            "tint": payload.tint,
            "mode": payload.mode,
            "grade_node": "white_balance",
            "processor": "nagar.color.whitebalance.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, graded_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": clip_rec.asset_id,
            "temperature_k": payload.temperature_k,
            "tint": payload.tint,
            "mode": payload.mode,
            "content_sha256": graded_record.content_sha256,
        },
    )


def _hdr_tonemap(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for color.hdr_tonemap."""
    payload = HdrTonemapInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"color.hdr_tonemap references unknown clip asset: {payload.clip_asset_id!r}"
        )
    clip_rec = known[payload.clip_asset_id]
    if clip_rec.media_kind != "video":
        raise CommandValidationError(
            f"color.hdr_tonemap requires video asset, got: {clip_rec.media_kind!r}"
        )
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_tonemap"
    digest_seed = (
        f"{clip_rec.content_sha256}:tonemap:{payload.source_space}:"
        f"{payload.target_space}:{payload.exposure_ev}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()
    graded_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=clip_rec.duration_us,
        parent_asset_ids=(clip_rec.asset_id,),
        provenance={
            "source_asset_id": clip_rec.asset_id,
            "source_space": payload.source_space,
            "target_space": payload.target_space,
            "exposure_ev": payload.exposure_ev,
            "grade_node": "hdr_tonemap",
            "processor": "nagar.color.tonemap.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, graded_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": clip_rec.asset_id,
            "source_space": payload.source_space,
            "target_space": payload.target_space,
            "exposure_ev": payload.exposure_ev,
            "content_sha256": graded_record.content_sha256,
        },
    )


def _deband_denoise(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for color.deband_denoise."""
    payload = DebandDenoiseInput.model_validate(context.input_data)
    known = _asset_index(project)
    if payload.clip_asset_id not in known:
        raise CommandValidationError(
            f"color.deband_denoise references unknown clip asset: {payload.clip_asset_id!r}"
        )
    clip_rec = known[payload.clip_asset_id]
    if clip_rec.media_kind != "video":
        raise CommandValidationError(
            f"color.deband_denoise requires video asset, got: {clip_rec.media_kind!r}"
        )
    output_id = payload.output_asset_id or f"{payload.clip_asset_id}_deband"
    digest_seed = (
        f"{clip_rec.content_sha256}:deband:{payload.quality_profile}:"
        f"{payload.temporal_radius}"
    )
    derived_sha256 = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()
    cleaned_record = AssetRecord(
        asset_id=output_id,
        media_kind="video",
        content_sha256=f"sha256:{derived_sha256}",
        duration_us=clip_rec.duration_us,
        parent_asset_ids=(clip_rec.asset_id,),
        provenance={
            "source_asset_id": clip_rec.asset_id,
            "quality_profile": payload.quality_profile,
            "temporal_radius": payload.temporal_radius,
            "grade_node": "deband_denoise",
            "processor": "nagar.color.deband.v1",
        },
    )
    new_project = project.model_copy(update={"assets": [*project.assets, cleaned_record]})
    return OperationOutcome(
        new_project,
        context.history,
        {
            "asset_id": output_id,
            "source_asset_id": clip_rec.asset_id,
            "quality_profile": payload.quality_profile,
            "temporal_radius": payload.temporal_radius,
            "content_sha256": cleaned_record.content_sha256,
        },
    )
```

Registration (in `register_delivery_operations`, domain `DOMAIN_COLOR`,
capability = operation short name, `PermissionLevel.REVERSIBLE`,
`required_packs=(DELIVERY_PACKAGE_ID,)`, `deterministic=True` — exactly the
file's existing shape), plus the three imports in the models import block and
the module-docstring bullet list.

## 4. Patch C — `pack.manifest.json`: three capabilities

Append to `capabilities` (order: after `color.match_shot`, keeping the color
family together):

```json
"color.white_balance",
"color.hdr_tonemap",
"color.deband_denoise",
```

Registered total becomes 80 operations / 8 packs (no `runtime.py` change is
needed — the delivery registrar is picked up by the wave-5 composition as-is).

## 5. Patch D — `tests/unit/test_delivery_pack.py`: contract + execution tests

Per operation (mirror the existing `_apply_lut` tests):

* input validation (temperature/tint bounds; literal enums; unknown fields);
* unknown clip asset → `CommandValidationError`; non-video asset → same;
* deterministic derived asset id + `sha256:` content hash; same input twice →
  same `content_sha256`;
* failure purity (failed dispatch leaves project state untouched).

## 6. Patch E — `delivery.export_otio` delegates to `creative/otio` (one call site)

**Why:** two OTIO implementations exist today. The delivery handler builds
tracks from *assets* (not timeline clips), ignores `include_markers`
**and** `timeline_id`, and hardcodes the record's `media_kind="video"`.
`creative/otio` is the canonical bridge (timeline → clips/gaps/markers,
`Marker.2` palette, documented `LOSS_CONTRACT`, real-library round-trip
tests). Target: ONE OTIO ADAPTER.

**Delegation** (replaces the `_export_otio` body after the payload line; the
`OtioClip`/`OtioTrack`/`RationalTime`/`TimeRange` models stay for
backwards-compatible imports until a follow-up removes them):

```python
def _export_otio(project: Project, context: OperationContext) -> OperationOutcome:
    """Level B (REVERSIBLE) handler for delivery.export_otio.

    Thin delegation to the canonical bridge
    (:mod:`nexus_ai_agent.creative.otio`); this handler owns only the pack
    contract (input model, asset record, output keys).
    """
    from nexus_ai_agent.creative.otio import dumps_otio_document, project_to_otio_document

    payload = ExportOtioInput.model_validate(context.input_data)
    document = project_to_otio_document(
        project, frame_rate=payload.frame_rate, include_markers=payload.include_markers
    )
    otio_json = dumps_otio_document(document)
    # ... keep the existing AssetRecord derivation + output keys, counting
    # total_video_clips/total_audio_clips from the parsed document tracks
    # (or from project assets during a transition release — reviewer decides).
```

**Behaviour changes requiring reviewer sign-off:**

1. `include_markers=False` starts working (today silently ignored).
2. `timeline_id` should select the exported timeline; today it is ignored. The
   minimal honest options are (a) validate it against
   `project.timeline.timeline_id` and raise `CommandValidationError` on
   mismatch — needs a one-line update to `test_export_otio_execution`, which
   dispatches `timeline_id="main"` against a `tl_delivery` fixture; or
   (b) keep ignoring it and document the single-timeline limitation. This
   handoff recommends (a).
3. Track/clip content changes from asset-derived to timeline-derived (the
   point of the delegation): golden `otio_json` assertions in
   `test_delivery_pack.py` must be re-based to the canonical document, and the
   rebound test must assert `Marker.2` emission with `include_markers=True`
   (task-121 style, no OTIO library needed).

**No import-cycle risk:** `creative/otio` imports only `creative.studio`
contracts; the delivery pack already imports those.

## 7. Integration notes (post-merge checklist)

1. Apply A→B→C→D, then run `pytest tests/unit/test_delivery_pack.py
   tests/unit/test_delivery_signing.py tests/architecture/test_delivery_pack_boundary.py`.
2. `composition_issues() == ()`, `stale_capabilities() == {}`,
   `nexus packs list` shows 10 delivery capabilities, 80 ops total.
3. `python scripts/pack_coverage.py` — delivery stays ≥95% (extend D if not).
4. Apply E, re-base the OTIO goldens, run `pytest
   tests/unit/test_otio_roundtrip.py` (11 tests; 2 need the optional library).
5. Update `creative/execution.py`: the three new ops are `EFFECT_DESCRIPTION`
   with `MISSING_RENDER_PRIMITIVES` entries (`white_balance` recommends the
   `ExposureOp` twin with `exposure_ev=0`; `hdr_tonemap` needs a tonemap
   LaneOp; `deband_denoise` needs an hqdn3d/gradfun LaneOp). The execution
   table test fails loudly until they are classified — that is the ratchet.
6. Board: release the `task-166`/`task-169` next-work items when this lands.
