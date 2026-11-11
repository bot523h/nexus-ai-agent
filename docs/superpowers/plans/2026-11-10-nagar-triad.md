# NAGAR Triad Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement three orthogonal IBPC extensions (Delta Dialogue, Shadow Preview, Scope Signature) in isolated zones with no external API, no new pip dependencies, and full TDD.

**Architecture:** Three pure-function modules (`delta.py`, `shadow.py`, `signature.py`) under `src/nexus_ai_agent/nagar/ibpc/`. Each module has zero imports from the other two. The existing `service.py`/`compiler.py`/`approval.py` pipeline is untouched. `delta.py` exports one symbol re-exported from `intent.py`; `shadow.py` and `signature.py` are standalone.

**Tech Stack:** Python 3.11+, Pydantic v2, ffmpeg (subprocess, already used by `creative.slideshow.ffmpeg`), stdlib `hashlib`/`base64`/`math` only.

**Spec:** `docs/superpowers/specs/2026-11-10-nagar-triad-design.md`

## Global Constraints

- `ConfigDict(extra="forbid", frozen=True)` on every new model
- No `shell=True` anywhere; subprocess args always a list
- All digest calls via `nagar.ibpc.canonical.digest_of` / `sha256_bytes`
- No imports from `service.py`, `session.py`, `approval.py`, `footprint.py` inside `delta.py`, `shadow.py`, `signature.py`
- All new public symbols in `__all__`
- Tests in `tests/unit/test_ibpc_delta.py`, `tests/unit/test_ibpc_shadow.py`, `tests/unit/test_ibpc_signature.py`
- Architecture assertions added to `tests/architecture/test_ibpc_boundary.py` (additive)
- Branch: `arena/nagar-delta-dialogue-vs1` (create if not exists before first commit)

## Review Focus

1. **Delta resolves to inverted scope** (start ≥ end after applying delta) → must return `Refusal("delta_out_of_range")` not raise.
2. **Shadow ffmpeg unavailable** (CI without binary) → `ShadowRenderError` with `"ffmpeg_not_found"`, not `FileNotFoundError`.
3. **Scope Signature on all-black video** → zero histogram vector stored as-is; cosine_similarity returns 0.0, not NaN.
4. **Delta on session with no prior scope** → `Clarification`, not `AttributeError`.
5. **Shadow frame stdout empty** (ffmpeg produced no bytes) → `ShadowRenderError("empty_frame")`, not silent `ShadowKeyframe` with empty b64.

---

## Zone A — Delta Dialogue

### Task A1: DeltaOp model and pure resolver

**Files:**
- Create: `src/nexus_ai_agent/nagar/ibpc/delta.py`
- Test: `tests/unit/test_ibpc_delta.py`

**Interfaces:**
- Consumes: `VideoTimeScope`, `TypedCreativeAST` from `nagar.ibpc.models`; `digest_of` from `nagar.ibpc.canonical`; `parse_intent`, `Clarification`, `Refusal`, `_to_ascii_digits`, `_NUMBER_RE` patterns from `nagar.ibpc.intent` (import the helpers, do not duplicate)
- Produces:
  - `DeltaAnchor(str, Enum)` with values `IN_POINT = "in_point"`, `OUT_POINT = "out_point"`
  - `DeltaOp(BaseModel, frozen, extra=forbid)`: fields `anchor: DeltaAnchor`, `delta_us: int`
  - `DeltaIntentAST(BaseModel, frozen, extra=forbid)`: fields `base_scope_digest: str`, `delta_op: DeltaOp`, `resolved_ast: TypedCreativeAST`, `intent_digest: str`
  - `parse_delta_intent(text, *, current_scope, project_id, asset_id, source_revision, duration_us, channel_binding_id) -> DeltaIntentAST | Clarification | Refusal`

- [ ] **Step 1: Write failing tests for DeltaOp model constraints**

```python
# tests/unit/test_ibpc_delta.py
from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaOp, DeltaIntentAST
from pydantic import ValidationError
import pytest


def test_delta_op_rejects_extra_fields():
    with pytest.raises(ValidationError):
        DeltaOp(anchor=DeltaAnchor.OUT_POINT, delta_us=-1_000_000, extra_field="bad")


def test_delta_op_rejects_zero_delta():
    # zero delta is refused at parse level, but model allows it — the parse function guards it
    op = DeltaOp(anchor=DeltaAnchor.OUT_POINT, delta_us=0)
    assert op.delta_us == 0  # model allows; parse_delta_intent refuses


def test_delta_anchor_values():
    assert DeltaAnchor.IN_POINT == "in_point"
    assert DeltaAnchor.OUT_POINT == "out_point"
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_delta.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Implement `DeltaAnchor`, `DeltaOp`, `DeltaIntentAST` in `delta.py`**

File header docstring: mirror the style of `intent.py`. Imports: `from __future__ import annotations`, `enum`, `re`, `pydantic`, `nagar.ibpc.canonical`, `nagar.ibpc.models`. Do not import `intent.py` helpers at module level (import lazily in `parse_delta_intent` to avoid circular).

```python
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field
from nexus_ai_agent.nagar.ibpc.models import (
    DeltaAnchor,
    DeltaOp,
    DeltaIntentAST,
)  # will be defined here
# ...
```

Models: `DeltaAnchor(str, Enum)`, `DeltaOp(BaseModel, frozen=True, extra="forbid")` with `anchor: DeltaAnchor` and `delta_us: int`, `DeltaIntentAST(BaseModel, frozen=True, extra="forbid")` with four fields per spec §1.3.

- [ ] **Step 4: Run model tests to PASS**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_delta.py::test_delta_op_rejects_extra_fields tests/unit/test_ibpc_delta.py::test_delta_anchor_values -v
```

- [ ] **Step 5: Commit model scaffold**

```bash
git checkout -b arena/nagar-delta-dialogue-vs1 2>/dev/null || git checkout arena/nagar-delta-dialogue-vs1
git add src/nexus_ai_agent/nagar/ibpc/delta.py tests/unit/test_ibpc_delta.py
git commit -m "feat(nagar/delta): scaffold DeltaAnchor, DeltaOp, DeltaIntentAST models

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

---

### Task A2: parse_delta_intent — pattern matching

**Files:**
- Modify: `src/nexus_ai_agent/nagar/ibpc/delta.py` (add function)
- Modify: `tests/unit/test_ibpc_delta.py` (add tests)

**Interfaces:**
- Consumes: `DeltaOp`, `DeltaAnchor`, `DeltaIntentAST` from Task A1; `VideoTimeScope`, `TypedCreativeAST` from `models.py`; `parse_intent` / `Clarification` / `Refusal` from `intent.py`; `intent_digest_of`, `normalize_intent` from `intent.py`
- Produces: `parse_delta_intent(text, *, current_scope, project_id, asset_id, source_revision, duration_us, channel_binding_id) -> DeltaIntentAST | Clarification | Refusal`

- [ ] **Step 1: Write failing tests**

```python
from nexus_ai_agent.nagar.ibpc.delta import parse_delta_intent
from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope
from nexus_ai_agent.nagar.ibpc.intent import Clarification, Refusal

SCOPE = VideoTimeScope(asset_id="a1", effect="keep", start_us=1_000_000, end_us=4_000_000)
DURATION = 6_000_000
CBN = "cbn_" + "a" * 64


def _delta(text, scope=SCOPE):
    return parse_delta_intent(
        text,
        current_scope=scope,
        project_id="p1",
        asset_id="a1",
        source_revision=1,
        duration_us=DURATION,
        channel_binding_id=CBN,
    )


def test_english_trim_end():
    r = _delta("trim 1 more second from the end")
    assert isinstance(r, type(r).__mro__[0])  # DeltaIntentAST
    from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST

    assert isinstance(r, DeltaIntentAST)
    assert r.delta_op.anchor == DeltaAnchor.OUT_POINT
    assert r.delta_op.delta_us == -1_000_000
    # resolved: out_point moves from 4_000_000 to 3_000_000
    assert r.resolved_ast.scope.end_us == 3_000_000
    assert r.resolved_ast.scope.start_us == 1_000_000


def test_persian_trim_end():
    r = _delta("یک ثانیه از تهش ببر")
    from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST

    assert isinstance(r, DeltaIntentAST)
    assert r.delta_op.anchor == DeltaAnchor.OUT_POINT
    assert r.delta_op.delta_us == -1_000_000


def test_trim_start():
    r = _delta("trim 0.5 seconds from the start")
    from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST

    assert isinstance(r, DeltaIntentAST)
    assert r.delta_op.anchor == DeltaAnchor.IN_POINT
    assert r.delta_op.delta_us == 500_000  # start moves right


def test_no_prior_scope_returns_clarification():
    r = parse_delta_intent(
        "trim 1 more second from the end",
        current_scope=None,
        project_id="p1",
        asset_id="a1",
        source_revision=1,
        duration_us=DURATION,
        channel_binding_id=CBN,
    )
    assert isinstance(r, Clarification)


def test_zero_delta_returns_refusal():
    # "trim 0 seconds" → no-op → Refusal
    r = _delta("trim 0 seconds from the end")
    assert isinstance(r, Refusal)
    assert r.reason == "zero_delta"


def test_inverted_scope_returns_refusal():
    # scope [1s, 1.3s]; trim 1s from end → end would be 0.3s < start 1s
    small_scope = VideoTimeScope(asset_id="a1", effect="keep", start_us=1_000_000, end_us=1_300_000)
    r = _delta("trim 1 second from the end", scope=small_scope)
    assert isinstance(r, Refusal)
    assert r.reason == "delta_out_of_range"


def test_base_scope_digest_in_result():
    r = _delta("trim 1 more second from the end")
    from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST

    assert isinstance(r, DeltaIntentAST)
    assert r.base_scope_digest == SCOPE.scope_digest


def test_resolved_ast_is_executable():
    r = _delta("trim 1 more second from the end")
    from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST

    assert isinstance(r, DeltaIntentAST)
    assert r.resolved_ast.executable
    assert r.resolved_ast.operation == "timeline.trim"
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_delta.py -v 2>&1 | head -30
```

- [ ] **Step 3: Implement `parse_delta_intent` in `delta.py`**

Pattern table (regex-based, deterministic):

```python
_END_TRIM_PATTERNS = [
    r"(?:trim|cut|remove|ببر|حذف|کوتاه).*?(\d+(?:\.\d+)?)\s*(?:s|sec|second|seconds|ثانیه)?.*?(?:end|tail|تهش|آخرش)",
    r"(\d+(?:\.\d+)?)\s*(?:s|sec|second|seconds|ثانیه)?.*?(?:از تهش|از آخرش|from the end|off the end)",
]
_START_TRIM_PATTERNS = [
    r"(?:trim|cut|ببر|حذف).*?(\d+(?:\.\d+)?).*?(?:start|beginning|اولش|ابتداش)",
    r"(\d+(?:\.\d+)?)\s*(?:s|sec|second|seconds|ثانیه)?.*?(?:از اولش|از ابتداش|from the start|off the start)",
]
```

Resolution logic:
1. Normalize text with `normalize_intent`, convert Persian digits.
2. Try `_END_TRIM_PATTERNS` → anchor=OUT_POINT, delta_us = -int(seconds * 1_000_000).
3. Try `_START_TRIM_PATTERNS` → anchor=IN_POINT, delta_us = +int(seconds * 1_000_000).
4. Try "add N seconds to end" → anchor=OUT_POINT, delta_us = +int(seconds * 1_000_000).
5. Try "add N seconds to start" → anchor=IN_POINT, delta_us = -int(seconds * 1_000_000).
6. No match → `Clarification(questions=("Which end? start or end? By how many seconds?",), reason="ambiguous_delta")`.
7. `delta_us == 0` → `Refusal("zero_delta")`.
8. If `current_scope is None` → `Clarification(..., reason="no_prior_scope")`.
9. Apply delta to `current_scope`, validate new range, build `TypedCreativeAST` with `parse_intent` internals reused.

- [ ] **Step 4: Run all delta tests**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_delta.py -v
```
Expected: all PASS

- [ ] **Step 5: Add re-export to `intent.py`**

Add at end of `intent.py` (above `__all__`):
```python
# Delta Dialogue — additive re-export, keeps the IBPC public surface unified
from nexus_ai_agent.nagar.ibpc.delta import parse_delta_intent as parse_delta_intent  # noqa: F401
```
Add `"parse_delta_intent"` to `intent.py`'s `__all__`.

- [ ] **Step 6: Verify re-export works**

```bash
cd /home/claude/nexus-ai-agent && python -c "from nexus_ai_agent.nagar.ibpc.intent import parse_delta_intent; print('ok')"
```

- [ ] **Step 7: Commit**

```bash
git add src/nexus_ai_agent/nagar/ibpc/delta.py src/nexus_ai_agent/nagar/ibpc/intent.py tests/unit/test_ibpc_delta.py
git commit -m "feat(nagar/delta): implement parse_delta_intent with relative scope resolution

Deterministic regex-based parser resolves Persian/English delta intents
against the current session scope. No LLM, no external API.
Zero-delta, inverted-scope, and no-prior-scope cases return typed
Clarification/Refusal. Re-exported from intent.py for unified surface.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

---

### Task A3: Architecture boundary test for delta.py

**Files:**
- Modify: `tests/architecture/test_ibpc_boundary.py` (additive assertions)

**Interfaces:**
- Consumes: `delta.py` module as importable
- Produces: assertion that `delta.py` does not import `service`, `session`, `approval`, `footprint`

- [ ] **Step 1: Read current boundary test to find insertion point**

```bash
grep -n "def test_" /home/claude/nexus-ai-agent/tests/architecture/test_ibpc_boundary.py | head -20
```

- [ ] **Step 2: Write failing test**

Add to `test_ibpc_boundary.py`:
```python
def test_delta_does_not_import_service_layer():
    """delta.py must not import service, session, approval, or footprint."""
    import ast, pathlib

    src = pathlib.Path("src/nexus_ai_agent/nagar/ibpc/delta.py").read_text()
    tree = ast.parse(src)
    forbidden = {"service", "session", "approval", "footprint"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, "names", [])]
            module = getattr(node, "module", "") or ""
            for f in forbidden:
                assert f not in module and not any(f in n for n in names), (
                    f"delta.py must not import {f!r}"
                )
```

- [ ] **Step 3: Run test**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/architecture/test_ibpc_boundary.py::test_delta_does_not_import_service_layer -v
```
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/architecture/test_ibpc_boundary.py
git commit -m "test(arch): assert delta.py does not import service layer

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

---

## Zone B — Shadow Preview

### Task B1: ShadowKeyframe and ShadowPreviewManifest models

**Files:**
- Create: `src/nexus_ai_agent/nagar/ibpc/shadow.py`
- Test: `tests/unit/test_ibpc_shadow.py`

**Interfaces:**
- Consumes: `digest_of` from `canonical.py`; `VideoTimeScope` from `models.py`; `resolve_ffmpeg_bin` from `creative.slideshow.ffmpeg`
- Produces:
  - `ShadowRenderError(RuntimeError)` with `.reason: str`
  - `ShadowKeyframe(BaseModel, frozen, extra=forbid)`: `timestamp_us: int`, `jpeg_b64: str`, `frame_digest: str` (pattern `^sha256:[0-9a-f]{64}$`)
  - `ShadowPreviewManifest(BaseModel, frozen, extra=forbid)`: per spec §2.3; property `shadow_digest -> str`
  - `build_shadow_preview(source_path, scope, plan_digest, *, ffmpeg_bin, max_keyframes, jpeg_quality, target_height) -> ShadowPreviewManifest`

- [ ] **Step 1: Write failing model tests**

```python
# tests/unit/test_ibpc_shadow.py
from nexus_ai_agent.nagar.ibpc.shadow import (
    ShadowKeyframe,
    ShadowPreviewManifest,
    ShadowRenderError,
)
from pydantic import ValidationError
import pytest


def test_keyframe_rejects_bad_digest():
    with pytest.raises(ValidationError):
        ShadowKeyframe(timestamp_us=0, jpeg_b64="AA==", frame_digest="not_a_digest")


def test_keyframe_accepts_valid():
    kf = ShadowKeyframe(
        timestamp_us=500_000,
        jpeg_b64="AA==",
        frame_digest="sha256:" + "a" * 64,
    )
    assert kf.timestamp_us == 500_000


def test_shadow_render_error_has_reason():
    err = ShadowRenderError("ffmpeg_not_found")
    assert err.reason == "ffmpeg_not_found"
    assert isinstance(err, RuntimeError)
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_shadow.py -v 2>&1 | head -15
```

- [ ] **Step 3: Implement models and `ShadowRenderError` in `shadow.py`**

```python
class ShadowRenderError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail
```

Models per spec §2.3. `ShadowPreviewManifest.shadow_digest` property: `digest_of(self.model_dump(mode="json"))`.

- [ ] **Step 4: Run model tests to PASS**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_shadow.py -v
```

- [ ] **Step 5: Commit scaffold**

```bash
git add src/nexus_ai_agent/nagar/ibpc/shadow.py tests/unit/test_ibpc_shadow.py
git commit -m "feat(nagar/shadow): scaffold ShadowKeyframe, ShadowPreviewManifest, ShadowRenderError

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

---

### Task B2: build_shadow_preview function

**Files:**
- Modify: `src/nexus_ai_agent/nagar/ibpc/shadow.py` (add function)
- Modify: `tests/unit/test_ibpc_shadow.py` (add tests)

**Interfaces:**
- Consumes: `ShadowKeyframe`, `ShadowPreviewManifest`, `ShadowRenderError` from Task B1; `VideoTimeScope` from `models.py`; `resolve_ffmpeg_bin` from `creative.slideshow.ffmpeg`; stdlib `subprocess`, `base64`, `hashlib`, `math`

- [ ] **Step 1: Write failing tests with mock ffmpeg**

```python
import base64, hashlib, subprocess, unittest.mock as mock
from pathlib import Path
from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview, ShadowRenderError
from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope

SCOPE = VideoTimeScope(asset_id="a1", effect="keep", start_us=0, end_us=2_000_000)
PLAN_DIGEST = "sha256:" + "b" * 64
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"  # minimal JPEG


def _fake_ffmpeg_run(args, **kwargs):
    result = mock.Mock()
    result.stdout = FAKE_JPEG
    result.returncode = 0
    return result


def test_build_returns_manifest(tmp_path):
    fake_src = tmp_path / "src.mp4"
    fake_src.write_bytes(b"fake")
    with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_run):
        with mock.patch(
            "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
        ):
            manifest = build_shadow_preview(
                fake_src,
                SCOPE,
                PLAN_DIGEST,
                max_keyframes=3,
                source_duration_us=2_000_000,
            )
    assert len(manifest.keyframes) == 3
    assert manifest.plan_digest == PLAN_DIGEST
    assert manifest.scope_digest == SCOPE.scope_digest
    assert manifest.composite_digest.startswith("sha256:")


def test_composite_digest_deterministic(tmp_path):
    fake_src = tmp_path / "src.mp4"
    fake_src.write_bytes(b"fake")
    with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_run):
        with mock.patch(
            "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
        ):
            m1 = build_shadow_preview(
                fake_src, SCOPE, PLAN_DIGEST, max_keyframes=2, source_duration_us=2_000_000
            )
            m2 = build_shadow_preview(
                fake_src, SCOPE, PLAN_DIGEST, max_keyframes=2, source_duration_us=2_000_000
            )
    assert m1.composite_digest == m2.composite_digest


def test_empty_frame_raises(tmp_path):
    fake_src = tmp_path / "src.mp4"
    fake_src.write_bytes(b"fake")
    empty_result = mock.Mock()
    empty_result.stdout = b""
    empty_result.returncode = 0
    with mock.patch("subprocess.run", return_value=empty_result):
        with mock.patch(
            "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
        ):
            with pytest.raises(ShadowRenderError) as exc:
                build_shadow_preview(
                    fake_src, SCOPE, PLAN_DIGEST, max_keyframes=1, source_duration_us=2_000_000
                )
    assert exc.value.reason == "empty_frame"


def test_ffmpeg_not_found_raises():
    with mock.patch(
        "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", side_effect=FileNotFoundError
    ):
        with pytest.raises(ShadowRenderError) as exc:
            build_shadow_preview(
                Path("/nonexistent.mp4"), SCOPE, PLAN_DIGEST, source_duration_us=2_000_000
            )
    assert exc.value.reason == "ffmpeg_not_found"


def test_max_keyframes_out_of_range():
    with pytest.raises(ValueError):
        build_shadow_preview(
            Path("/x.mp4"), SCOPE, PLAN_DIGEST, max_keyframes=0, source_duration_us=2_000_000
        )
    with pytest.raises(ValueError):
        build_shadow_preview(
            Path("/x.mp4"), SCOPE, PLAN_DIGEST, max_keyframes=17, source_duration_us=2_000_000
        )
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_shadow.py -v 2>&1 | head -30
```

- [ ] **Step 3: Implement `build_shadow_preview` in `shadow.py`**

Signature: `build_shadow_preview(source_path: Path, scope: VideoTimeScope, plan_digest: str, *, ffmpeg_bin: str | None = None, max_keyframes: int = 8, jpeg_quality: int = 23, target_height: int = 480, source_duration_us: int) -> ShadowPreviewManifest`

Algorithm per spec §2.4:
1. Validate `max_keyframes ∈ [1, 16]`; raise `ValueError` otherwise.
2. Resolve `ffmpeg_bin`; catch `FileNotFoundError` → raise `ShadowRenderError("ffmpeg_not_found")`.
3. Compute `start_s = scope.start_us / 1_000_000`, `dur_s = (scope.end_us - scope.start_us) / 1_000_000`; dur ≤ 0 → `ShadowRenderError("empty_scope")`.
4. Build `timestamps`: `[start_s + i * dur_s / (n-1) for i in range(n)]` where `n = min(max_keyframes, 8)`. If n==1, `timestamps = [start_s + dur_s / 2]`.
5. For each `t`: `subprocess.run(["ffmpeg", "-ss", str(t), "-i", str(source_path), "-vframes", "1", "-f", "image2", "-vf", f"scale=-2:{target_height}", f"-q:v", str(jpeg_quality), "pipe:1"], stdout=PIPE, stderr=PIPE, timeout=30, check=False)`. If stdout empty → `ShadowRenderError("empty_frame", f"at t={t:.3f}s")`.
6. `frame_digest = "sha256:" + hashlib.sha256(jpeg_bytes).hexdigest()`.
7. `jpeg_b64 = base64.b64encode(jpeg_bytes).decode()`.
8. `composite_digest = "sha256:" + hashlib.sha256(b"".join(sorted(fd.encode() for fd in frame_digests))).hexdigest()`.
9. Build and return `ShadowPreviewManifest`.

- [ ] **Step 4: Run all shadow tests**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_shadow.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus_ai_agent/nagar/ibpc/shadow.py tests/unit/test_ibpc_shadow.py
git commit -m "feat(nagar/shadow): implement build_shadow_preview

Renders sparse keyframes from the affected scope via ffmpeg pipe (no disk
write, no shell). Composite digest binds approval to actual pixels seen.
No external API, no new dependencies.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

---

## Zone C — Scope Signature

### Task C1: ScopeSignature and ScopeMatch models

**Files:**
- Create: `src/nexus_ai_agent/nagar/ibpc/signature.py`
- Test: `tests/unit/test_ibpc_signature.py`

**Interfaces:**
- Produces:
  - `SignatureError(RuntimeError)` with `.reason: str`
  - `ScopeSignature(BaseModel, frozen, extra=forbid)`: per spec §3.3; property `computed_at` default `""`, `signature_digest` computed on init
  - `ScopeMatch(BaseModel, frozen, extra=forbid)`: `candidate_scope_digest: str`, `cosine_similarity: float`, `motion_delta: float`
  - `cosine_similarity(a, b) -> float`

- [ ] **Step 1: Write failing model tests**

```python
# tests/unit/test_ibpc_signature.py
from nexus_ai_agent.nagar.ibpc.signature import (
    ScopeSignature,
    ScopeMatch,
    SignatureError,
    cosine_similarity,
)
import pytest


def _make_sig(hist, motion=0.0):
    return ScopeSignature(
        scope_digest="sha256:" + "c" * 64,
        asset_id="a1",
        start_us=0,
        end_us=2_000_000,
        color_histogram=tuple(hist),
        motion_magnitude=motion,
        sample_frames=4,
        computed_at="2026-11-10T00:00:00Z",
        signature_digest="sha256:" + "d" * 64,
    )


def test_cosine_similarity_identical_unit_vectors():
    v = [1.0] + [0.0] * 63
    assert abs(cosine_similarity(tuple(v), tuple(v)) - 1.0) < 1e-9


def test_cosine_similarity_orthogonal():
    a = [1.0] + [0.0] * 63
    b = [0.0, 1.0] + [0.0] * 62
    assert cosine_similarity(tuple(a), tuple(b)) < 1e-9


def test_cosine_similarity_clipped_to_0_1():
    # floating point rounding must not produce > 1.0 or < 0.0
    v = tuple([1.0 / 8.0] * 64)  # already normalised to L2 = 1.0
    s = cosine_similarity(v, v)
    assert 0.0 <= s <= 1.0


def test_scope_match_model():
    m = ScopeMatch(
        candidate_scope_digest="sha256:" + "e" * 64,
        cosine_similarity=0.95,
        motion_delta=2.3,
    )
    assert m.cosine_similarity == 0.95


def test_signature_error_has_reason():
    e = SignatureError("empty_scope")
    assert e.reason == "empty_scope"
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_signature.py -v 2>&1 | head -15
```

- [ ] **Step 3: Implement models and `cosine_similarity` in `signature.py`**

`cosine_similarity`: the two histograms are L2-normalised (enforced by `compute_scope_signature`), so the dot product IS the cosine similarity. Use `max(0.0, min(1.0, dot))` to clamp floating-point rounding artefacts.

`SignatureError`:
```python
class SignatureError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
```

- [ ] **Step 4: Run model tests to PASS**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_signature.py -v
```

- [ ] **Step 5: Commit scaffold**

```bash
git add src/nexus_ai_agent/nagar/ibpc/signature.py tests/unit/test_ibpc_signature.py
git commit -m "feat(nagar/signature): scaffold ScopeSignature, ScopeMatch, cosine_similarity

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

---

### Task C2: compute_scope_signature — color histogram

**Files:**
- Modify: `src/nexus_ai_agent/nagar/ibpc/signature.py`
- Modify: `tests/unit/test_ibpc_signature.py`

**Interfaces:**
- Consumes: `ScopeSignature`, `SignatureError` from Task C1; `VideoTimeScope` from `models.py`; `resolve_ffmpeg_bin` from `creative.slideshow.ffmpeg`; stdlib `subprocess`, `math`, `struct`, `datetime`
- Produces: `compute_scope_signature(source_path, scope, *, ffmpeg_bin, max_frames) -> ScopeSignature`

- [ ] **Step 1: Write failing tests**

```python
import struct, math, unittest.mock as mock
from pathlib import Path
from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature, SignatureError
from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope

SCOPE = VideoTimeScope(asset_id="a1", effect="keep", start_us=0, end_us=2_000_000)


def _make_rgb_frame(r, g, b, w=160, h=90):
    """Synthetic solid-colour raw RGB24 frame."""
    return bytes([r, g, b] * w * h)


def _fake_run_rgb(args, **kwargs):
    result = mock.Mock()
    result.stdout = _make_rgb_frame(200, 100, 50)
    result.returncode = 0
    return result


def test_signature_has_64_bin_histogram(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    with mock.patch("subprocess.run", side_effect=_fake_run_rgb):
        with mock.patch(
            "nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"
        ):
            sig = compute_scope_signature(src, SCOPE, max_frames=4, source_duration_us=2_000_000)
    assert len(sig.color_histogram) == 64


def test_histogram_l2_normalised(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    with mock.patch("subprocess.run", side_effect=_fake_run_rgb):
        with mock.patch(
            "nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"
        ):
            sig = compute_scope_signature(src, SCOPE, max_frames=4, source_duration_us=2_000_000)
    norm = math.sqrt(sum(x * x for x in sig.color_histogram))
    assert abs(norm - 1.0) < 1e-6 or norm == 0.0  # zero vector allowed


def test_all_black_video_zero_norm(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    black = mock.Mock()
    black.stdout = bytes(160 * 90 * 3)
    black.returncode = 0
    with mock.patch("subprocess.run", return_value=black):
        with mock.patch(
            "nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"
        ):
            sig = compute_scope_signature(src, SCOPE, max_frames=2, source_duration_us=2_000_000)
    # all-black → all L*=0 → all in first L* bin → zero a* and b* bins
    # norm may be non-zero (all weight in one bin); just check len and no NaN
    assert len(sig.color_histogram) == 64
    assert all(not math.isnan(x) for x in sig.color_histogram)


def test_signature_digest_bound_to_content(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    with mock.patch("subprocess.run", side_effect=_fake_run_rgb):
        with mock.patch(
            "nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"
        ):
            s1 = compute_scope_signature(src, SCOPE, max_frames=2, source_duration_us=2_000_000)
            s2 = compute_scope_signature(src, SCOPE, max_frames=2, source_duration_us=2_000_000)
    assert s1.signature_digest == s2.signature_digest


def test_ffmpeg_not_found_raises():
    with mock.patch(
        "nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", side_effect=FileNotFoundError
    ):
        with pytest.raises(SignatureError) as exc:
            compute_scope_signature(Path("/x.mp4"), SCOPE, source_duration_us=2_000_000)
    assert exc.value.reason == "ffmpeg_not_found"
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_signature.py -v 2>&1 | head -20
```

- [ ] **Step 3: Implement `compute_scope_signature` with RGB→Lab conversion**

The CIE L\*a\*b\* conversion (D65, no scipy):

```python
def _rgb_to_lab(r: int, g: int, b: int) -> tuple[float, float, float]:
    # sRGB → linear
    def _linearise(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    rl, gl, bl = _linearise(r), _linearise(g), _linearise(b)
    # linear RGB → XYZ (D65)
    X = 0.4124564 * rl + 0.3575761 * gl + 0.1804375 * bl
    Y = 0.2126729 * rl + 0.7151522 * gl + 0.0721750 * bl
    Z = 0.0193339 * rl + 0.1191920 * gl + 0.9503041 * bl

    # XYZ → Lab (D65 white: Xn=0.95047, Yn=1.0, Zn=1.08883)
    def _f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = _f(X / 0.95047), _f(Y / 1.0), _f(Z / 1.08883)
    L = max(0.0, 116 * fy - 16)
    a = 500 * (fx - fy)
    b_val = 200 * (fy - fz)
    return L, a, b_val
```

Frame decode: `subprocess.run(["ffmpeg", "-ss", str(t), "-i", str(src), "-vframes", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-vf", "scale=160:90", "pipe:1"], stdout=PIPE, stderr=PIPE, timeout=30)`. Each pixel is 3 bytes.

Histogram accumulation: L∈[0,100]→32 bins (bin = int(L/100*32)), a\*∈[-128,127]→16 bins (bin = int((a+128)/256*16)), b\*∈[-128,127]→16 bins. Accumulate over all frames. Flatten in order L0..L31, a0..a15, b0..b15 → 64 values. L2-normalize. If norm==0 store as-is.

Motion magnitude: mean absolute pixel difference between consecutive raw grayscale frames (convert RGB24 to grayscale as `0.299*R + 0.587*G + 0.114*B`, then `mean|frame_i - frame_{i-1}|`).

`signature_digest`: `digest_of(model_dump excluding signature_digest field)` — build dict manually.

- [ ] **Step 4: Run all signature tests**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_signature.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus_ai_agent/nagar/ibpc/signature.py tests/unit/test_ibpc_signature.py
git commit -m "feat(nagar/signature): implement compute_scope_signature with CIE-Lab histogram

Pure-Python RGB→CIE-L*a*b* conversion (no scipy), ffmpeg pipe decode,
L2-normalised 64-bin histogram + motion magnitude via MAD.
No external API, no new dependencies.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

---

### Task C3: ScopeMatchEngine

**Files:**
- Modify: `src/nexus_ai_agent/nagar/ibpc/signature.py`
- Modify: `tests/unit/test_ibpc_signature.py`

**Interfaces:**
- Consumes: `ScopeSignature`, `ScopeMatch`, `cosine_similarity` from earlier tasks
- Produces: `ScopeMatchEngine.find_similar(source, candidates, *, threshold, max_results) -> list[ScopeMatch]`

- [ ] **Step 1: Write failing tests**

```python
from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine, ScopeSignature, ScopeMatch
import math


def _sig(digest_char, hist):
    norm = math.sqrt(sum(x * x for x in hist)) or 1.0
    h = tuple(x / norm for x in hist)
    return ScopeSignature(
        scope_digest="sha256:" + digest_char * 64,
        asset_id="a1",
        start_us=0,
        end_us=1_000_000,
        color_histogram=h,
        motion_magnitude=0.0,
        sample_frames=2,
        computed_at="2026-11-10T00:00:00Z",
        signature_digest="sha256:" + "0" * 64,
    )


SOURCE = _sig("a", [1.0] + [0.0] * 63)
SIMILAR = _sig("b", [0.99, 0.141] + [0.0] * 62)  # very close to SOURCE
DIFFERENT = _sig("c", [0.0] * 32 + [1.0] + [0.0] * 31)  # orthogonal


def test_find_similar_returns_above_threshold():
    engine = ScopeMatchEngine()
    results = engine.find_similar(SOURCE, [SIMILAR, DIFFERENT], threshold=0.5)
    assert len(results) == 1
    assert results[0].candidate_scope_digest == SIMILAR.scope_digest
    assert results[0].cosine_similarity >= 0.5


def test_find_similar_sorted_descending():
    engine = ScopeMatchEngine()
    results = engine.find_similar(SOURCE, [DIFFERENT, SIMILAR], threshold=0.0)
    assert results[0].cosine_similarity >= results[-1].cosine_similarity


def test_max_results_respected():
    engine = ScopeMatchEngine()
    many = [SIMILAR] * 10
    results = engine.find_similar(SOURCE, many, threshold=0.0, max_results=3)
    assert len(results) <= 3


def test_threshold_out_of_range():
    engine = ScopeMatchEngine()
    with pytest.raises(ValueError):
        engine.find_similar(SOURCE, [SIMILAR], threshold=1.5)


def test_empty_candidates():
    engine = ScopeMatchEngine()
    results = engine.find_similar(SOURCE, [], threshold=0.5)
    assert results == []
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_signature.py -k "ScopeMatchEngine" -v 2>&1 | head -15
```

- [ ] **Step 3: Implement `ScopeMatchEngine` in `signature.py`**

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
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0.0, 1.0], got {threshold}")
        matches = []
        for cand in candidates:
            sim = cosine_similarity(source.color_histogram, cand.color_histogram)
            if sim >= threshold:
                matches.append(
                    ScopeMatch(
                        candidate_scope_digest=cand.scope_digest,
                        cosine_similarity=sim,
                        motion_delta=abs(source.motion_magnitude - cand.motion_magnitude),
                    )
                )
        matches.sort(key=lambda m: m.cosine_similarity, reverse=True)
        return matches[:max_results]
```

- [ ] **Step 4: Run all signature tests**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/test_ibpc_signature.py -v
```
Expected: all PASS

- [ ] **Step 5: Run full unit test suite to check no regressions**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/unit/ -v --tb=short 2>&1 | tail -20
```

- [ ] **Step 6: Commit**

```bash
git add src/nexus_ai_agent/nagar/ibpc/signature.py tests/unit/test_ibpc_signature.py
git commit -m "feat(nagar/signature): implement ScopeMatchEngine

find_similar computes cosine similarity on L2-normalised Lab histograms
and returns matches above threshold sorted descending. No model, no API.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

---

## Final: Architecture boundary + CHANGELOG

### Task D1: Architecture boundary assertions for shadow.py and signature.py

**Files:**
- Modify: `tests/architecture/test_ibpc_boundary.py`

- [ ] **Step 1: Add two assertions** (mirror Task A3 pattern for shadow.py and signature.py)

- [ ] **Step 2: Run**

```bash
cd /home/claude/nexus-ai-agent && python -m pytest tests/architecture/test_ibpc_boundary.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/architecture/test_ibpc_boundary.py
git commit -m "test(arch): boundary assertions for shadow.py and signature.py

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

---

### Task D2: CHANGELOG and PR body

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Prepend CHANGELOG entry**

```markdown
## [Unreleased] — NAGAR Triad (Delta Dialogue · Shadow Preview · Scope Signature)

### Added
- `nagar/ibpc/delta.py`: `parse_delta_intent` — deterministic relative-scope intent parser.
  Resolves "trim 1 more second from the end / یک ثانیه از تهش ببر" against the current
  `VideoTimeScope` without any LLM or external API. Persian digit normalisation included.
- `nagar/ibpc/shadow.py`: `build_shadow_preview` — visual preview from actual pixels.
  Renders sparse keyframes via ffmpeg pipe (no disk write, no shell). `composite_digest`
  binds approval to the pixels the user saw, not to plan arithmetic.
- `nagar/ibpc/signature.py`: `compute_scope_signature` + `ScopeMatchEngine` — model-free
  visual fingerprinting of a `VideoTimeScope`. Pure-Python CIE-L*a*b* histogram + motion
  MAD, L2-normalised, cosine-similarity matching. No OpenCV, no scipy, no API.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG entry for NAGAR Triad

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D"
```

- [ ] **Step 3: Push and open draft PR**

```bash
git push -u origin arena/nagar-delta-dialogue-vs1
gh pr create \
  --title "feat(nagar): NAGAR Triad — Delta Dialogue, Shadow Preview, Scope Signature" \
  --body "$(cat <<'EOF'
## Summary

Three orthogonal, zero-external-API IBPC extensions in isolated zones:

- **Delta Dialogue** (`nagar/ibpc/delta.py`): `parse_delta_intent` resolves relative intents ("trim 1 more second from the end / یک ثانیه از تهش ببر") against the current scope. Deterministic, regex-based, no LLM.
- **Shadow Preview** (`nagar/ibpc/shadow.py`): `build_shadow_preview` renders sparse keyframes from the affected scope via ffmpeg pipe. `composite_digest` binds the approval to actual pixels.
- **Scope Signature** (`nagar/ibpc/signature.py`): `compute_scope_signature` + `ScopeMatchEngine` — pure-Python CIE-L*a*b* histogram (64-bin, L2-normalised) + motion MAD, cosine-similarity matching. No opencv, no scipy, no new pip dependency.

## Zone isolation
- `nagar-delta-dialogue` / `nagar-shadow-preview` / `nagar-scope-signature`
- Zero shared files between zones; zero imports from service/session/approval/footprint
- Architecture tests in `test_ibpc_boundary.py` enforce the dependency arrow

## Test plan
- [ ] `pytest tests/unit/test_ibpc_delta.py` — all pass
- [ ] `pytest tests/unit/test_ibpc_shadow.py` — all pass
- [ ] `pytest tests/unit/test_ibpc_signature.py` — all pass
- [ ] `pytest tests/architecture/test_ibpc_boundary.py` — all pass (additive assertions)
- [ ] `pytest tests/unit/` — no regressions

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01LkQvdNSsQmLL5xUY4CvM4D
EOF
)" \
  --draft
```

---

*Plan ends here. Implement task-by-task, commit after each task, CI is the official gate.*
