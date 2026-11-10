# NAGAR Triad: Delta Dialogue · Shadow Preview · Scope Signature
## Design Specification — 2026-11-10

> **Status:** Approved for implementation  
> **Zones:** `nagar-delta-dialogue` · `nagar-shadow-preview` · `nagar-scope-signature`  
> **Constraint:** No external API, no LLM, no new pip installs; all logic built from primitives already in the repo.

---

## 0. Context

The existing IBPC pipeline compiles free-text intent to a typed AST, validates it against the registry, builds a plan-derived (non-visual) preview, waits for a single-use digest-bound approval, enqueues one `creative_render` job, drains, runs the independent footprint verifier, and seals a capsule.

Three orthogonal, non-overlapping extensions sit entirely within the `nagar/ibpc/` module boundary:

| Extension | Zone | Files created | Files touched |
|---|---|---|---|
| **Delta Dialogue** | `nagar-delta-dialogue` | `delta.py` | `intent.py` (additive export only) |
| **Shadow Preview** | `nagar-shadow-preview` | `shadow.py` | `models.py` (additive model only) |
| **Scope Signature** | `nagar-scope-signature` | `signature.py` | none |

The three zones share **zero** files. No task needs another zone's output. Each zone produces independently shippable, independently testable software.

---

## 1. Delta Dialogue

### 1.1 Problem

The existing `parse_intent` requires the caller to state an absolute range (`trim from X to Y seconds`). A user mid-session who says *"take one more second off the end"* has no deterministic way to express this — the parser must know the **currently accepted scope** to resolve the delta. Without session-scope awareness the parser either asks a clarifying question or fabricates a number. Both are wrong.

### 1.2 Invariant

> A delta intent is always compiled against the last **consumed** (executed) `VideoTimeScope` of the current session. If no prior scope exists, the parse returns a typed `Clarification` asking for an absolute range.

### 1.3 Data model

```python
class DeltaAnchor(str, Enum):
    IN_POINT = "in_point"  # move the start of the kept window
    OUT_POINT = "out_point"  # move the end of the kept window


class DeltaOp(BaseModel, frozen=True, extra="forbid"):
    """One relative mutation on the in- or out-point of the last committed scope."""

    anchor: DeltaAnchor
    delta_us: int  # signed microseconds; negative = move earlier


class DeltaIntentAST(BaseModel, frozen=True, extra="forbid"):
    """Typed result of parse_delta_intent — structurally identical to TypedCreativeAST
    but carries the resolved absolute scope so the existing compiler path is unchanged."""

    base_scope_digest: str  # sha256 of the scope this delta was applied to
    delta_op: DeltaOp
    resolved_ast: TypedCreativeAST  # ready to pass to compile_ast unchanged
    intent_digest: str
```

### 1.4 Parser rules (deterministic, no model)

| Input pattern | Anchor | Sign |
|---|---|---|
| "trim \[N\] more seconds from the end / از تهش" | OUT_POINT | negative |
| "add \[N\] seconds back at the end" | OUT_POINT | positive |
| "trim \[N\] more seconds from the start / از اولش" | IN_POINT | positive |
| "add \[N\] seconds back at the start" | IN_POINT | negative |
| "move \[in/start\] \[earlier/later\] by N" | IN_POINT | ±1 |
| "move \[out/end\] \[earlier/later\] by N" | OUT_POINT | ±1 |

If no anchor/sign can be resolved → `Clarification`. If resolved scope would invert or produce a range ≤ 0 → `Refusal("delta_out_of_range")`.

### 1.5 Integration

`parse_delta_intent(text, *, current_scope, project_id, asset_id, source_revision, duration_us, channel_binding_id) → DeltaIntentAST | Clarification | Refusal`

The returned `DeltaIntentAST.resolved_ast` is a plain `TypedCreativeAST` and passes unchanged to `compile_ast`. No change to `compiler.py`, `service.py`, or `models.py`.

The only change to `intent.py` is one additive re-export: `from nexus_ai_agent.nagar.ibpc.delta import parse_delta_intent`.

### 1.6 Bounds & guards

- `delta_us` must be non-zero; zero delta is a `Refusal("zero_delta")`.
- Resolved `start_us` must be `>= 0`; resolved `end_us` must be `<= duration_us`.
- Max absolute delta: `duration_us / 2` (a delta larger than half the asset is likely a misparse).
- Persian digit normalization reuses `_to_ascii_digits` from `intent.py`.

---

## 2. Shadow Preview

### 2.1 Problem

The current `PreviewManifest.kind = "plan_derived"` is computed from the plan's arithmetic (frame map + output duration), never from real pixels. The user sees numbers, not images. For short clips this is acceptable; for a 10-second trim of a 2-minute interview it is not — the user cannot see which cut they are approving.

### 2.2 Invariant

> The approval digest must bind to the pixels the user actually saw. If the shadow render changes (re-encode, codec drift), the previous approval is invalid.

### 2.3 Data model (additive to `models.py`)

```python
class ShadowKeyframe(BaseModel, frozen=True, extra="forbid"):
    timestamp_us: int  # source timestamp of this frame
    jpeg_b64: str  # base64-encoded JPEG, 480p, quality-factor 23
    frame_digest: str  # sha256(raw JPEG bytes) — pattern ^sha256:[0-9a-f]{64}$


class ShadowPreviewManifest(BaseModel, frozen=True, extra="forbid"):
    """Visual preview: a sparse set of keyframes from the affected scope only.

    composite_digest = sha256(concat of sorted frame_digests) — this is what
    the approval binds to, not plan_digest. The sort is by timestamp_us so the
    digest is order-independent within the scope.
    """

    schema_id: Literal["nexus.ibpc.shadow_preview.v1"]
    shadow_id: str  # default_factory: _new_id("shad")
    plan_digest: str  # ^sha256:...
    scope_digest: str  # ^sha256:...
    source_duration_us: int
    scope_start_us: int
    scope_end_us: int
    keyframes: tuple[ShadowKeyframe, ...]
    composite_digest: str  # ^sha256:...  — approval binds this

    @property
    def shadow_digest(self) -> str: ...  # digest_of(model_dump)
```

### 2.4 Render algorithm (`shadow.py`)

```
build_shadow_preview(
    source_path: Path,
    scope: VideoTimeScope,
    plan_digest: str,
    *,
    ffmpeg_bin: str | None = None,
    max_keyframes: int = 8,
    jpeg_quality: int = 23,
    target_height: int = 480,
) -> ShadowPreviewManifest
```

1. Resolve scope → `start_s`, `duration_s` (float, from `start_us / 1_000_000`).
2. Determine keyframe timestamps: evenly spaced within `[start_s, start_s + duration_s)`, capped at `max_keyframes`.
3. For each timestamp: `ffmpeg -ss {t} -i {source} -vframes 1 -f image2 -vf scale=-2:{target_height} -q:v {jpeg_quality} pipe:1` → stdout bytes.
4. Compute `frame_digest = "sha256:" + hashlib.sha256(jpeg_bytes).hexdigest()`.
5. `composite_digest = "sha256:" + sha256(b"".join(sorted(fd.encode() for fd in frame_digests))).hexdigest()`.
6. Return `ShadowPreviewManifest(keyframes=..., composite_digest=...)`.

**No new binary dependency.** Uses `resolve_ffmpeg_bin` already in `creative.slideshow.ffmpeg`.  
**No write to disk.** All frames flow through stdout pipes.  
**Timeout:** 30 s per frame, 120 s total; exceeded → `ShadowRenderError`.

### 2.5 Integration

`shadow.py` is a pure function library. `service.py` and `approval.py` are **not** changed by this zone. Any caller that wants a visual preview calls `build_shadow_preview` and may store `ShadowPreviewManifest` alongside the existing `PreviewManifest`; the approval pipeline is unaffected because it currently binds `plan_digest` and callers who want to bind `composite_digest` instead must do so at the application layer (outside IBPC core — that is a future task).

### 2.6 Bounds & guards

- `max_keyframes` ∈ [1, 16]; outside → `ValueError`.
- `scope_duration_s <= 0` → `ShadowRenderError("empty_scope")`.
- Each ffmpeg invocation is a **subprocess** (no shell), args as a list, stdout=PIPE, stderr=PIPE, timeout enforced.
- `jpeg_bytes` empty → `ShadowRenderError("empty_frame")`.
- `ShadowKeyframe.jpeg_b64` stores `base64.b64encode(jpeg_bytes).decode()`.

---

## 3. Scope Signature

### 3.1 Problem

A user who graded the first 5 seconds of one clip and wants *"the same look on similar scenes"* today has no way to express this without a model. The model-free alternative: define **similarity** as a measurable distance between two scopes' visual and motion fingerprints, computed entirely from FFmpeg output.

### 3.2 Invariant

> A `ScopeSignature` is a deterministic, model-free, verifiable fingerprint of a `VideoTimeScope`. Two signatures whose `cosine_similarity` is ≥ a caller-supplied threshold are "visually similar" by the system's definition. The definition is explicit, documented, and testable.

### 3.3 Data model

```python
class ScopeSignature(BaseModel, frozen=True, extra="forbid"):
    """Deterministic, model-free visual + motion fingerprint of one scope."""

    scope_digest: str  # sha256 of the VideoTimeScope
    asset_id: str
    start_us: int
    end_us: int
    # 64-bin CIE-L*a*b* histogram (L: 32 bins, a*: 16, b*: 16) — flattened
    color_histogram: tuple[float, ...]  # len == 64, L2-normalised
    # mean per-frame optical-flow magnitude (Farneback via OpenCV-free fallback)
    motion_magnitude: float
    # number of frames sampled
    sample_frames: int
    computed_at: str  # ISO-8601 UTC
    signature_digest: str  # sha256(model_dump except signature_digest)


class ScopeMatch(BaseModel, frozen=True, extra="forbid"):
    candidate_scope_digest: str
    cosine_similarity: float  # [0.0, 1.0]
    motion_delta: float  # |source.motion_magnitude - candidate.motion_magnitude|
```

### 3.4 Computation (`signature.py`)

**Color histogram (no OpenCV required):**
1. Sample up to `max_frames` evenly across the scope using ffmpeg `-vf scale=160:90,format=rgb24`.
2. Convert each pixel RGB → CIE L\*a\*b\* using the pure-Python formula (D65 illuminant, no scipy dependency — the formula is a closed-form float computation under 20 lines).
3. Accumulate 64-bin histogram: L∈[0,100]→32 bins, a\*∈[-128,127]→16 bins, b\*∈[-128,127]→16 bins.
4. L2-normalize the histogram vector.

**Motion magnitude (OpenCV-free fallback):**
1. Sample pairs of adjacent frames, compute mean absolute pixel difference (MAD) between consecutive grayscale frames.
2. `motion_magnitude = mean(MAD over all adjacent frame pairs)`. Range [0, 255]. This is not optical flow but is deterministic, fast, and suffices for coarse similarity.

**Cosine similarity:**
```python
def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    # vectors are already L2-normalized → dot product IS cosine similarity
    return max(0.0, min(1.0, dot))
```

**`ScopeMatchEngine`:**
```python
class ScopeMatchEngine:
    def find_similar(
        self,
        source: ScopeSignature,
        candidates: Sequence[ScopeSignature],
        *,
        threshold: float = 0.80,
        max_results: int = 5,
    ) -> list[ScopeMatch]:
        """Return candidates whose cosine_similarity >= threshold, sorted descending."""
```

### 3.5 Bounds & guards

- `max_frames` ∈ [2, 32]; default 8.
- `scope_duration_s <= 0` → `SignatureError("empty_scope")`.
- Histogram must be L2-normalized before storage; a zero vector (all-black video) is stored as-is with `cosine_similarity = 0.0` against any other.
- `threshold` ∈ [0.0, 1.0]; outside → `ValueError`.
- No file writes. All frame decoding through ffmpeg subprocess stdout pipe, same pattern as `shadow.py`.

---

## 4. Global Constraints

- Python ≥ 3.11 (repo minimum from `pyproject.toml`)
- All new models: `ConfigDict(extra="forbid", frozen=True)`
- All subprocess calls: list args, no `shell=True`, timeout enforced
- `digest_of` and `sha256_bytes` from `nagar.ibpc.canonical` — no new hashing imports
- No imports from `service.py`, `session.py`, `approval.py`, or `footprint.py` within `delta.py`, `shadow.py`, `signature.py` (dependency arrow goes one way: service → these modules, never reverse)
- All new public functions and classes exported in `__all__`
- Tests follow existing patterns: `tests/unit/test_ibpc_*.py` for unit, `tests/architecture/test_ibpc_boundary.py` to be extended (additive assertions only)

---

## 5. Non-Goals (this spec)

- Wiring `ShadowPreviewManifest` into the approval binding (future task, different zone)
- Persisting `ScopeSignature` objects across sessions (future task)
- Full optical-flow via OpenCV (requires install; blocked by no-new-deps constraint)
- Look Cascade orchestrator (depends on lf/perception layer not on main)
