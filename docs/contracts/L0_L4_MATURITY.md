# L0–L4 Maturity Model — Canonical Definitions

> **ADR:** [0007-l0-l4-maturity](../architecture/adr/0007-l0-l4-maturity.md)  
> **Evidence date:** 2026-09-24  
> **Repo scan:** `grep -R "L0|L1|L2|L3|L4|maturity" src/ docs/ → 0 results before Gate 2`

## Why this model exists

Gate 2 mission requires distinguishing:

* PRODUCT_DEFINED
* REGISTERED
* DOMAIN_REDUCER_READY
* EXECUTOR_READY
* SURFACE_MAPPED
* COMMAND_CONTRACTED
* TESTED
* PROVEN

A single status is insufficient. Example:

* PRODUCT_DEFINED=true, REGISTERED=true, DOMAIN_REDUCER_READY=true, EXECUTOR_READY=false → registered but not executable.

## Canonical Levels

### L0 — Concept / Unimplemented

* **Required evidence:** TDD entry or product catalog mention
* **Allowed claims:** PRODUCT_DEFINED
* **Transition:** TDD defines operation_id, pack, input/output shape — no code
* **Example:** `portrait.smooth_skin` defined in TDD but no registry entry (in current runtime it is MISSING, but conceptually L0)
* **Evidence class:** DOCUMENTED_ONLY

### L1 — Contract or Registry Presence

* **Required evidence:**
  * OperationSpec registered in CapabilityRegistry
  * pack.manifest.json declares capability
  * TypedCommand operation_id accepted by bus (UnknownOperationError not raised)
* **Allowed claims:** PRODUCT_DEFINED, REGISTERED, COMMAND_CONTRACTED
* **Transition:** Registry knows operation_id; manifest verifies; no handler required
* **Example:** `timeline.trim` registered, manifest capability present, but handler not proven (hypothetical)
* **Evidence class:** SUPPORTED

### L2 — Domain / State Behavior Proven

* **Required evidence:**
  * Pure handler (Project, OperationContext) -> OperationOutcome exists
  * Unit test proves state transition (no I/O)
  * EffectLayerRef or Timeline patch computed deterministically
* **Allowed claims:** PRODUCT_DEFINED, REGISTERED, DOMAIN_REDUCER_READY, COMMAND_CONTRACTED, TESTED
* **Transition:** Handler exists, is pure, and has deterministic unit test
* **Example:** `timeline.ripple_delete` pure reducer test in test_edit_pack.py
* **Evidence class:** OBSERVED

### L3 — Real Execution / Instrument Proven

* **Required evidence:**
  * Render lane compiles IR -> filtergraph -> argv (or real audio/caption execution)
  * Real encode or probe test (imageio-ffmpeg, ffprobe, audio analysis)
  * No subprocess spawned from pack handler (lane is only executor)
* **Allowed claims:** PRODUCT_DEFINED, REGISTERED, DOMAIN_REDUCER_READY, EXECUTOR_READY, COMMAND_CONTRACTED, TESTED, PROVEN
* **Transition:** Real execution artifact produced (video, audio, transcript, OTIO)
* **Example:** `delivery.make_proxy_480p` renders real mp4 via render lane
* **Evidence class:** VERIFIED

### L4 — End-to-End Product / Surface Proof

* **Required evidence:**
  * Telegram surface mapping (/edit, /caption, /grade) or API endpoint
  * Idempotency + auth + locality policy enforced
  * User-visible output in bot or dashboard
* **Allowed claims:** PRODUCT_DEFINED, REGISTERED, DOMAIN_REDUCER_READY, EXECUTOR_READY, SURFACE_MAPPED, COMMAND_CONTRACTED, TESTED, PROVEN
* **Transition:** Surface handler calls bus, worker executes, notifier delivers
* **Example:** `media.play` wired via bot surface + bus + worker (P0-B)
* **Evidence class:** VERIFIED

## Computation

```python
from nexus_ai_agent.creative.contracts.l0_l4 import compute_l_level

level = compute_l_level(
    registered=True,
    domain_reducer_ready=True,
    executor_ready=False,
    surface_mapped=False,
    tested=True,
    proven=False,
)
# → L2
```

## Current Distribution (2026-09-24)

* L0: 23 operations (portrait 10 + scene 10 + color 3) — missing in runtime
* L1: 0 (all registered ops have at least reducer)
* L2: ~30 operations (pure handlers, no real encode proof)
* L3: ~20 operations (real encode via render lane or audio/caption)
* L4: ~7 operations (media.play, media.pause, timeline.mark, split_at_playhead, system.undo, slideshow.compose, slideshow.render)

Total: 57 runtime + 23 missing = 80 rows (70 catalog + 10 extra)

## Zero Hallucination Rule

* Never claim L3 because L2 exists.
* Never claim L4 because L3 exists.
* Each level requires its own evidence class.
