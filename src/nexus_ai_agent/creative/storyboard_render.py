"""``storyboard.render``: the fail-closed V1 vertical slice, screenplay to verified MP4.

Six steps, in order. The first failure stops the job and nothing partial is
left at a final path:

1. **Request validation.** :func:`validate_storyboard_render_request` (in
   ``nagar/creative/handoff.py``) checks shape, finiteness and the
   screenplay digest before any compile work.
2. **Compile and seal.** :func:`compile_storyboard` runs, then
   :func:`verify_manifest_integrity` recomputes every digest. If a trust anchor
   key is configured, it is required and checked.
3. **Single-use approval.** The approval is consumed from the existing
   :class:`ApprovalLedger` (ibpc). Its identity binds ``project_id``,
   ``source_sha256``, ``manifest_sha256`` and ``render_profile_hash``, and the
   turn id ties it to one job. A replay, a changed field or a revoked approval
   is refused. Consumption happens after the digests are known, so a bad
   request does not spend the approval.
4. **Materialize.** :class:`StoryboardMediaMaterializer` writes the slates,
   tones and manifest atomically under ``projects/<project>/renders/<job>/``.
5. **Provenance seal (TOCTOU).** ``verify_materialized_provenance`` runs
   immediately before FFmpeg.
6. **Render, verify, passport.** FFmpeg writes a temp MP4 in the same
   directory. The independent verifier inspects that temp file. Only on
   ``PASS`` is it replaced into place, and only then is the HMAC passport
   written. If the MP4 is still not ``PASS`` after the move, it is removed.

Authority note: the approval ledger is process-local (ibpc design). An approval
issued in one process cannot be consumed in another, so this handler must run
in the same process that issued the approval.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.artifact_passport import (
    PASSPORT_SUFFIX,
    build_passport,
    write_passport,
)
from nexus_ai_agent.creative.artifact_verifier import PASS, verify_mp4
from nexus_ai_agent.creative.materializer import (
    MaterializationError,
    MaterializedBundle,
    RenderProfile,
    StoryboardMediaMaterializer,
    stream_sha256,
)
from nexus_ai_agent.creative.storyboard_compiler import (
    compile_storyboard,
    verify_manifest_integrity,
)
from nexus_ai_agent.nagar.creative.handoff import (
    HandoffRefusal,
    validate_storyboard_render_request,
)
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError, ApprovalIdentity, ApprovalLedger
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding

RENDER_OUTPUT_BASENAME = "storyboard"
APPROVAL_TENANT_ID = "local"
APPROVAL_SEQUENCE = 1
APPROVAL_SOURCE_REVISION = 0
DEFAULT_FFMPEG_TIMEOUT_S = 180.0
#: Duration tolerance: one frame of quantisation per shot, plus a fixed margin.
DURATION_MARGIN_S = 0.05


class StoryboardRenderError(RuntimeError):
    """A typed, fail-closed refusal or failure of the render chain."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class RenderContext:
    """Host-injected authority: workspace, approval ledger, keys and binaries."""

    workspace_root: Path
    approval_ledger: ApprovalLedger
    signing_key: str | bytes
    hmac_key: str | bytes | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    clock: Callable[[], float] = time.time
    ffmpeg_timeout_s: float = DEFAULT_FFMPEG_TIMEOUT_S


def approval_identity_for(
    *,
    project_id: str,
    job_id: str,
    principal_id: str,
    source_sha256: str,
    script_sha256: str,
    manifest_sha256: str,
    render_profile_hash: str,
) -> ApprovalIdentity:
    """Map the storyboard binding onto the ibpc identity schema.

    ``source_asset_digest`` carries the screenplay digest, ``ast_digest`` the
    compiled script digest, ``plan_digest`` the sealed manifest digest and
    ``preview_digest`` the render profile digest. ``session_id`` and
    ``turn_id`` scope the approval to one project and one job.
    """
    return ApprovalIdentity(
        principal_id=principal_id,
        tenant_id=APPROVAL_TENANT_ID,
        project_id=project_id,
        session_id=f"studio:{project_id}",
        turn_id=f"storyboard.render:{job_id}",
        sequence_number=APPROVAL_SEQUENCE,
        source_revision=APPROVAL_SOURCE_REVISION,
        source_asset_digest=_prefixed(source_sha256),
        ast_digest=_prefixed(script_sha256),
        plan_digest=_prefixed(manifest_sha256),
        preview_digest=_prefixed(render_profile_hash),
    )


def _prefixed(hex64: str) -> str:
    """The ibpc binding stores digests as ``sha256:<64 hex>``."""
    return f"sha256:{hex64}"


def issue_render_approval(
    ledger: ApprovalLedger,
    *,
    project_id: str,
    job_id: str,
    principal_id: str,
    source_sha256: str,
    script_sha256: str,
    manifest_sha256: str,
    render_profile_hash: str,
) -> str:
    """Host-side issuance: create and approve one binding, return its id.

    The host is the human-decision point. It issues the approval after it has
    compiled and reviewed the same digests the handler will recompute.
    """
    identity = approval_identity_for(
        project_id=project_id,
        job_id=job_id,
        principal_id=principal_id,
        source_sha256=source_sha256,
        script_sha256=script_sha256,
        manifest_sha256=manifest_sha256,
        render_profile_hash=render_profile_hash,
    )
    binding = ApprovalBinding(
        session_id=identity.session_id,
        turn_id=identity.turn_id,
        sequence_number=APPROVAL_SEQUENCE,
        tenant_id=APPROVAL_TENANT_ID,
        project_id=project_id,
        principal_id=principal_id,
        source_revision=APPROVAL_SOURCE_REVISION,
        source_asset_digest=identity.source_asset_digest,
        ast_digest=identity.ast_digest,
        plan_digest=identity.plan_digest,
        preview_digest=identity.preview_digest,
    )
    stored = ledger.create(binding)
    ledger.approve(stored.approval_id, identity=identity, turn_is_active=lambda: True)
    return stored.approval_id


def build_ffmpeg_argv(
    ffmpeg: str,
    bundle: MaterializedBundle,
    output_path: str,
) -> list[str]:
    """Build the FFmpeg argv. Only numbers and materialized paths appear, never user text."""
    images: dict[str, str] = {}
    wavs: dict[str, str] = {}
    for record in bundle.assets:
        if record.kind in ("slate_png", "local_image"):
            images[record.shot_id] = record.resolved_path
        elif record.kind == "tone_wav":
            wavs[record.shot_id] = record.resolved_path
    shot_ids = list(images)
    if not shot_ids or set(shot_ids) != set(wavs):
        raise StoryboardRenderError("bundle_incomplete", "every shot needs an image and a tone")
    fps = f"{bundle.profile.fps:.6f}".rstrip("0").rstrip(".")
    argv: list[str] = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    filters: list[str] = []
    for index, (shot_id, duration) in enumerate(
        zip(shot_ids, bundle.shot_durations_s, strict=True)
    ):
        dur = f"{duration:.6f}"
        argv += ["-loop", "1", "-framerate", fps, "-t", dur, "-i", images[shot_id]]
        argv += ["-t", dur, "-i", wavs[shot_id]]
        filters.append(f"[{2 * index}:v]format=yuv420p,setsar=1,setpts=PTS-STARTPTS[v{index}]")
        filters.append(f"[{2 * index + 1}:a]asetpts=PTS-STARTPTS[a{index}]")
    count = len(shot_ids)
    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(count))
    filters.append(f"{concat_inputs}concat=n={count}:v=1:a=1[outv][outa]")
    argv += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        output_path,
    ]
    return argv


def _run_ffmpeg(argv: list[str], timeout_s: float, tmp_path: Path) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv built from validated paths, no shell
            argv,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        tmp_path.unlink(missing_ok=True)
        raise StoryboardRenderError("ffmpeg_timeout", f"after {timeout_s:.0f}s") from exc
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise StoryboardRenderError("ffmpeg_unavailable", type(exc).__name__) from exc
    if completed.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        tail = completed.stderr[-400:].decode("utf-8", errors="replace")
        raise StoryboardRenderError("ffmpeg_failed", f"rc={completed.returncode}: {tail}")


def render_storyboard_sync(payload: Mapping[str, Any], ctx: RenderContext) -> dict[str, Any]:
    """Run the six-step chain synchronously. Raises :class:`StoryboardRenderError`."""
    # 1. request validation
    try:
        req = validate_storyboard_render_request(payload)
    except HandoffRefusal as exc:
        raise StoryboardRenderError(exc.reason, exc.detail) from exc
    try:
        profile = RenderProfile.from_mapping(req.profile)
    except MaterializationError as exc:
        raise StoryboardRenderError(exc.reason, exc.detail) from exc

    # 2. compile and seal
    try:
        manifest = compile_storyboard(req.screenplay_text, hmac_key=ctx.hmac_key)
    except Exception as exc:  # noqa: BLE001 - every compile failure is a refusal here
        raise StoryboardRenderError("compile_failed", type(exc).__name__) from exc
    if not verify_manifest_integrity(
        manifest,
        expected_source_sha256=req.source_sha256,
        hmac_key=ctx.hmac_key,
        require_trusted_anchor=ctx.hmac_key is not None,
    ):
        raise StoryboardRenderError("manifest_integrity_failed")
    if req.manifest_sha256 is not None and req.manifest_sha256 != manifest.manifest_sha256:
        raise StoryboardRenderError("manifest_digest_mismatch")
    profile_hash = profile.profile_hash()

    # 3. single-use approval, identity-bound
    try:
        ctx.approval_ledger.consume(
            req.approval_id,
            identity=approval_identity_for(
                project_id=req.project_id,
                job_id=req.job_id,
                principal_id=req.principal_id,
                source_sha256=req.source_sha256,
                script_sha256=manifest.script_sha256,
                manifest_sha256=manifest.manifest_sha256,
                render_profile_hash=profile_hash,
            ),
        )
    except ApprovalError as exc:
        raise StoryboardRenderError(f"approval_{exc.reason}", exc.detail) from exc

    workspace = Path(ctx.workspace_root).resolve()
    materializer = StoryboardMediaMaterializer(workspace, profile)
    out_dir = workspace / "projects" / req.project_id / "renders" / req.job_id
    try:
        # 4. materialize
        bundle = materializer.materialize(manifest, out_dir)
        # 5. TOCTOU seal, immediately before FFmpeg
        materializer.verify_materialized_provenance(bundle)
    except MaterializationError as exc:
        raise StoryboardRenderError(exc.reason, exc.detail) from exc

    # 6. render to temp, verify the temp, then move it into place
    final_mp4 = Path(bundle.out_dir) / f"{RENDER_OUTPUT_BASENAME}.mp4"
    fd, tmp_name = tempfile.mkstemp(dir=bundle.out_dir, prefix=".storyboard.", suffix=".mp4.tmp")
    os.close(fd)
    tmp_mp4 = Path(tmp_name)
    moved = False
    try:
        argv = build_ffmpeg_argv(ctx.ffmpeg, bundle, str(tmp_mp4))
        _run_ffmpeg(argv, ctx.ffmpeg_timeout_s, tmp_mp4)
        tolerance = DURATION_MARGIN_S + len(bundle.shot_durations_s) / profile.fps
        verdict = verify_mp4(
            tmp_mp4,
            expected_duration_s=bundle.total_duration_s,
            tolerance_s=tolerance,
            ffprobe=ctx.ffprobe,
        )
        if not verdict.passed:
            raise StoryboardRenderError(
                f"verifier_{verdict.status.lower()}", "; ".join(verdict.reasons)
            )
        os.replace(tmp_mp4, final_mp4)
        moved = True
        if stream_sha256(final_mp4)[0] != verdict.output_sha256:
            raise StoryboardRenderError("artifact_changed_after_verify")
        # 7. passport, only after PASS and the post-move hash check
        passport = build_passport(
            job_id=req.job_id,
            project_id=req.project_id,
            source_sha256=req.source_sha256,
            manifest_sha256=manifest.manifest_sha256,
            render_profile_hash=profile_hash,
            asset_merkle_root=bundle.asset_merkle_root,
            total_asset_leaves=bundle.total_asset_leaves,
            output_rel_path=str(final_mp4.relative_to(workspace)),
            output_size_bytes=verdict.size_bytes,
            output_sha256=verdict.output_sha256,
            probed_duration_s=verdict.probed_duration_s,
            verifier_status=PASS,
            created_at_unix_s=ctx.clock(),
            signing_key=ctx.signing_key,
        )
        passport_path = Path(bundle.out_dir) / f"{RENDER_OUTPUT_BASENAME}{PASSPORT_SUFFIX}"
        write_passport(passport_path, passport)
    except StoryboardRenderError:
        tmp_mp4.unlink(missing_ok=True)
        if moved:
            final_mp4.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp_mp4.unlink(missing_ok=True)
        if moved:
            final_mp4.unlink(missing_ok=True)
        raise StoryboardRenderError("render_failed", type(exc).__name__) from exc

    return {
        "status": "rendered",
        "job_id": req.job_id,
        "project_id": req.project_id,
        "output_rel_path": passport["output_rel_path"],
        "output_sha256": verdict.output_sha256,
        "output_size_bytes": verdict.size_bytes,
        "probed_duration_s": verdict.probed_duration_s,
        "verifier_status": PASS,
        "manifest_sha256": manifest.manifest_sha256,
        "asset_merkle_root": bundle.asset_merkle_root,
        "total_asset_leaves": bundle.total_asset_leaves,
        "passport_rel_path": str(passport_path.relative_to(workspace)),
        "total_shots": manifest.total_shots,
        "shot_durations_s": list(bundle.shot_durations_s),
        "render_profile_hash": profile_hash,
        "approval_consumed": req.approval_id,
    }


def make_storyboard_render_handler(ctx: RenderContext) -> Callable[[dict[str, Any]], Any]:
    """Return an async queue handler bound to ``ctx``.

    The queue calls it with the job payload. The synchronous chain runs in a
    worker thread, so the event loop is not blocked by FFmpeg.
    """

    async def _handler(payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(render_storyboard_sync, payload, ctx)

    return _handler


__all__ = [
    "RENDER_OUTPUT_BASENAME",
    "RenderContext",
    "StoryboardRenderError",
    "approval_identity_for",
    "build_ffmpeg_argv",
    "issue_render_approval",
    "make_storyboard_render_handler",
    "render_storyboard_sync",
]
