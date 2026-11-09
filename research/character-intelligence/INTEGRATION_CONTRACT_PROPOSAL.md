# Integration Contract Proposal (Arena 1 ↔ Character Capsule)

Status: **PROPOSAL ONLY.** Nothing here is implemented against Arena 1, nothing in
Arena 1 was modified, and the adapters are `DEFERRED` until Arena 1's composition
runtime settles its own contracts.

## Principle

The capsule is a **source of truth about a character**; the composition runtime is
a **source of decisions about a shot**. Neither should own the other's data. The
contract is therefore narrow, versioned, and one-directional per call.

## Proposed surface (3 calls, 0 shared tables)

```python
# 1. Resolve: which capsule revision does this shot pin?
GET /v1/characters/{character_id}/revisions/{revision_no}
→ 200 {"capsule_digest": "sha256:…", "module_digests": {...}, "schema": "character-capsule/1"}

# 2. Verify: is this frame consistent with that capsule?
POST /v1/verify
    {"shot_id": "SH-109", "character_id": "NAGAR-CHAR-000001",
     "capsule_digest": "sha256:…", "gallery": ["AST-…", …],
     "image": "<content-addressed reference>", "timeline_scene": "SC-001"}
→ 200 {"schema": "character-capsule-continuity-report/1",
        "verdict": "IDENTITY_DRIFT", "dimensions": [...], "evidence_digest": "sha256:…",
        "calibration_digest": "sha256:…"}

# 3. Evidence: hand over the bundle for a verdict id
GET /v1/evidence/{evidence_digest} → the bundle JSON (content-addressed)
```

Properties:

* **No shared database, no shared queue, no shared registry.** Arena 1 keeps its
  asset registry; the capsule keeps its asset refs; they meet at a digest.
* **Idempotent**: verifying the same content twice yields the same
  `evidence_digest` (timestamps excluded from the claim digest).
* **Fail-closed**: an unknown `capsule_digest` is a 409, never a "best guess".
* **Read-only for Arena 1**: the capsule never writes into Arena 1's store.

## What Arena 1 would have to provide

| input | why | fallback if absent |
|---|---|---|
| shot id + scene id + sequence id + shot index | canon and wardrobe-plan checks | those dimensions become `NOT_MEASURED` |
| declared outfit id | wardrobe continuity | `WARDROBE_DRIFT` cannot be claimed; only garment *appearance* is compared |
| gallery asset ids | identity comparison | verification refuses (`ValidationError`) |
| scene environment (background colour, key temperature) | scene continuity | scene becomes `NOT_MEASURED` |

## What the capsule would provide to Arena 1

| output | shape | stability |
|---|---|---|
| verdict | one of the closed 8 states | **frozen** in `character-capsule-continuity-report/1` |
| dimension details | band, distance, score, measured flag | additive changes only |
| limitations | list of strings | additive |
| evidence digest | `sha256:…` | frozen |

## Explicitly *not* proposed

* No event bus topic, no CommandBus handler, no new queue.
* No changes to `src/nexus_ai_agent/creative/**`, `nagar/ibpc/**`, packs,
  rendering, or `job_queue.py`.
* No shared "Asset Registry v2": the capsule reuses existing asset ids as opaque
  references.
* No write-back of verdicts into Arena 1's database; a consumer stores the digest
  or nothing.

## Migration path (when Arena 1 is ready)

1. Arena 1 adds `capsule_ref` to its shot record (one nullable column, its decision).
2. A thin adapter in Arena 1 calls `/v1/verify` and stores `evidence_digest`.
3. Until step 1 lands, this mission's own CLI
   (`scripts/character_capsule/verify_shot.py`) is the integration surface — it
   proves the contract is usable without Arena 1 changes.

Status by step: 1 = **DEFERRED**, 2 = **DEFERRED**, 3 = **VERIFIED** (CLI exists and
runs).
