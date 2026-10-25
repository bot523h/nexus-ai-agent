"""The artifact-verification contract (task-178).

Runtime semantics are **used, not redefined**: the evidence functions are the
runtime's own (``creative.slideshow.ffmpeg.sha256_file`` / the allow-listed
binary probe), and the atomic publication rule belongs to the executor
(``.part`` staging → rename). This module is the Job layer's independent
*re-measurement* of what the runtime reports — verification reads the world,
it never trusts the handler's word.

Three identities (found in the tree, not assumed):

* **logical** — ``project_id = shot-<idempotency_key>`` and the pack
  operation's output asset id (``creative/render_jobs.py::_build_project``
  and the dispatch ``output.asset_id``);
* **spec** — the canonical operation id plus the compiled lane IR hash
  (``creative/rendering/compiler.py``: ``ir_hash = "sha256:" +
  sha256(canonical payload)`` — the LaneIR/spec identity);
* **physical** — output path (workspace-contained) + measured size +
  recomputed ``sha256:<hex>`` + probe evidence for media.

Canonical rule: a handler that claims success without a verifiable artifact
cannot complete a job. A result shaped ``{"success": False, "error_code":
...}`` is a *typed user failure* — since task-181 (GAP-A) it is a FAILURE of
the job: the queue classifies it (``jobs/failure_semantics``) and persists
``FAILED_RETRYABLE``/``FAILED_TERMINAL`` with ``typed_failure:<code>``; the
notifier translates the code. Verifiers apply the same rule fail-closed: if
a typed failure result ever reaches a verifier (it should not — the queue
short-circuits first), the verifier refuses it (``typed_user_failure``) —
no layer of the stack may let a non-success claim become ``COMPLETED``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Verification reason codes (persisted as ``verification_failed:<code>``).
REASON_MISSING = "missing_artifact"
REASON_EMPTY = "empty_artifact"
REASON_OUTSIDE_ROOT = "outside_expected_root"
REASON_UNEXPECTED_PATH = "unexpected_artifact_path"
REASON_NO_CLAIM = "success_without_artifact_claim"
REASON_SHA_FORMAT = "invalid_sha256_claim"
REASON_SHA_MISMATCH = "sha256_mismatch"
REASON_SIZE_MISMATCH = "size_mismatch"
REASON_PROBE_FAILED = "probe_failed"
REASON_DURATION_MISMATCH = "duration_mismatch"
#: task-181 (GAP-A): a typed user failure reached a verifier — refuse it.
REASON_TYPED_FAILURE = "typed_user_failure"

_TOLERANCE_US = 1_500_000  # claimed vs probed duration bound (same probe source)

_SRT_TIMING = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$", re.MULTILINE)

ProbeFn = Callable[[Path], dict[str, Any]]


class ArtifactClaimError(ValueError):
    """The handler claimed success but produced no usable artifact claim."""


@dataclass(frozen=True)
class ArtifactClaim:
    """What the handler *says* it produced — checked, never trusted."""

    path: Path
    kind: str  # video | srt | otio | binary
    sha256: str
    size_bytes: int | None
    duration_us: int | None
    operation: str
    output_asset_id: str

    @classmethod
    def from_handler_result(
        cls, result: dict[str, Any], *, default_kind: str = "binary"
    ) -> ArtifactClaim:
        """Read the documented result dialects of this repository.

        ``creative_render`` (``creative/render_jobs.py``): ``artifact_path``,
        ``artifact_kind``, ``sha256``.  ``slideshow_render``
        (``creative/slideshow/worker_adapter.py``): ``output_path``,
        ``content_sha256``.  Anything else is a contract error.
        """
        raw_path = result.get("artifact_path") or result.get("output_path")
        sha = result.get("sha256") or result.get("content_sha256")
        if not raw_path or not isinstance(raw_path, str):
            raise ArtifactClaimError(REASON_NO_CLAIM)
        if not sha or not isinstance(sha, str):
            raise ArtifactClaimError(REASON_NO_CLAIM)
        kind = result.get("artifact_kind")
        if not isinstance(kind, str) or not kind.strip():
            kind = _kind_from_suffix(Path(raw_path).suffix, default=default_kind)
        elif kind == "document":
            # The worker's coarse kind refines by suffix: a .srt is verified
            # as SubRip, a .otio as JSON — the documented artifact names of
            # the creative chain.
            kind = _kind_from_suffix(Path(raw_path).suffix, default="binary")
        duration = result.get("duration_us")
        size = result.get("size_bytes")
        return cls(
            path=Path(raw_path),
            kind=kind,
            sha256=sha,
            size_bytes=int(size) if isinstance(size, int) else None,
            duration_us=int(duration) if isinstance(duration, int) else None,
            operation=str(result.get("operation") or ""),
            output_asset_id=str(result.get("output_asset_id") or ""),
        )


def _kind_from_suffix(suffix: str, *, default: str) -> str:
    table = {
        ".mp4": "video",
        ".mov": "video",
        ".mkv": "video",
        ".webm": "video",
        ".srt": "srt",
        ".otio": "otio",
        ".json": "otio",
        # task-180 dialects (GAP-B/GAP-C): the feature lane's real artifacts.
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".webp": "image",
        ".txt": "text",
    }
    return table.get(suffix.lower(), default)


@dataclass(frozen=True)
class VerificationOutcome:
    """Result of the independent re-measurement."""

    ok: bool
    reason_code: str | None
    summary: dict[str, Any]

    @property
    def block(self) -> dict[str, Any]:
        """The queue-owned ``artifact_verification`` result block."""
        return self.summary


def default_media_probe(path: Path) -> dict[str, Any]:
    """Bind the RUNTIME's own probe as the media evidence source.

    ``probe_video`` is the architecture's ffprobe-equivalent (same
    allow-listed binary; raises on unreadable/zero-byte media), so a
    verification failure here is exactly "ffprobe failed" by runtime
    semantics. Imported lazily to keep this module import-light.
    """
    from nexus_ai_agent.creative.slideshow.ffmpeg import probe_video, resolve_ffmpeg_bin

    info = probe_video(path, binary=resolve_ffmpeg_bin())
    return {
        "duration_us": info.duration_us,
        "width": info.width,
        "height": info.height,
        "has_audio": info.has_audio,
    }


def verify_artifact(
    claim: ArtifactClaim,
    *,
    expected_path: Path | None = None,
    expected_root: Path | None = None,
    probe: ProbeFn | None = None,
) -> VerificationOutcome:
    """Independently re-measure the claimed artifact against §10 invariants.

    Invariants (all must hold for media *and* document artifacts):

    exists → size > 0 → is the expected artifact (exact path when the
    operation defines one, always contained in the job workspace) → stable
    identity (``sha256:<hex>`` recomputed from bytes equals the claim) →
    probe evidence for media → recorded logical/spec identity. The returned
    summary is what the Job Result carries, so the chain stays traceable.

    Dialects: ``video`` (allow-listed-binary probe), ``srt`` (SubRip timing
    grammar), ``otio`` (JSON document), and — added by task-180 for the
    feature lane's real artifacts — ``image`` (Pillow structural decode) and
    ``text`` (whole-file UTF-8 decode). Every dialect is deterministic and
    read-only; none trusts a handler-reported property.
    """
    logical = {"project_id": None, "output_asset_id": claim.output_asset_id}
    spec = {"operation": claim.operation}
    physical: dict[str, Any] = {"claimed_path": str(claim.path)}
    summary: dict[str, Any] = {
        "logical_identity": logical,
        "spec_identity": spec,
        "physical_identity": physical,
        "probe": None,
        "status": "failed",
    }

    def fail(code: str) -> VerificationOutcome:
        summary["reason_code"] = code
        return VerificationOutcome(ok=False, reason_code=code, summary=summary)

    if not claim.sha256.startswith("sha256:") or len(claim.sha256) != len("sha256:") + 64:
        return fail(REASON_SHA_FORMAT)

    path = claim.path
    physical["path"] = str(path)
    if expected_path is not None and path != expected_path:
        physical["expected_path"] = str(expected_path)
        return fail(REASON_UNEXPECTED_PATH)
    if expected_root is not None:
        root = expected_root.resolve()
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return fail(REASON_OUTSIDE_ROOT)
        physical["workspace"] = str(root)

    if not path.exists() or not path.is_file():
        return fail(REASON_MISSING)
    size = path.stat().st_size
    physical["size_bytes"] = size
    if size <= 0:
        return fail(REASON_EMPTY)
    if claim.size_bytes is not None and claim.size_bytes != size:
        return fail(REASON_SIZE_MISMATCH)

    from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file

    measured_sha = sha256_file(path)
    physical["sha256"] = measured_sha
    if measured_sha != claim.sha256:
        return fail(REASON_SHA_MISMATCH)

    if claim.kind == "video":
        probe_fn = probe or default_media_probe
        try:
            evidence = probe_fn(path)
        except Exception as exc:  # noqa: BLE001 - any probe failure is verification failure
            physical["probe_error"] = f"{type(exc).__name__}: {exc}"[:400]
            return fail(REASON_PROBE_FAILED)
        summary["probe"] = evidence
        if (
            claim.duration_us is not None
            and abs(int(evidence.get("duration_us") or 0) - claim.duration_us) > _TOLERANCE_US
        ):
            return fail(REASON_DURATION_MISMATCH)
    elif claim.kind == "srt":
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            physical["read_error"] = str(exc)[:400]
            return fail(REASON_PROBE_FAILED)
        if not _SRT_TIMING.search(text):
            return fail(REASON_PROBE_FAILED)
    elif claim.kind == "otio":
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            physical["read_error"] = str(exc)[:400]
            return fail(REASON_PROBE_FAILED)
        if not isinstance(document, dict):
            return fail(REASON_PROBE_FAILED)
    elif claim.kind == "image":
        # Structural validity = the bytes decode as a real image (task-180,
        # GAP-C: the story artifact is a PIL-rendered PNG). Pillow is a core
        # dependency, so this probe has no external-binary requirement.
        try:
            from PIL import Image

            with Image.open(path) as candidate:
                candidate.verify()  # structural integrity of the full file
            with Image.open(path) as candidate:
                # verify() consumes the handle; reopen for the measured facts.
                evidence = {
                    "format": candidate.format,
                    "width": candidate.width,
                    "height": candidate.height,
                    "mode": candidate.mode,
                }
        except Exception as exc:  # noqa: BLE001 - any decode failure is a verification failure
            physical["read_error"] = f"{type(exc).__name__}: {exc}"[:400]
            return fail(REASON_PROBE_FAILED)
        summary["probe"] = evidence
    elif claim.kind == "text":
        # Structural validity = the whole file decodes as UTF-8 (task-180,
        # GAP-B: the pdf_extract artifact is the extracted text layer).
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            physical["read_error"] = f"{type(exc).__name__}: {exc}"[:400]
            return fail(REASON_PROBE_FAILED)
        summary["probe"] = {"encoding": "utf-8", "characters": len(text)}

    summary["status"] = "verified"
    summary.pop("reason_code", None)
    summary["sha256"] = measured_sha
    summary["size_bytes"] = size
    return VerificationOutcome(ok=True, reason_code=None, summary=summary)


__all__ = [
    "ArtifactClaim",
    "ArtifactClaimError",
    "ProbeFn",
    "VerificationOutcome",
    "default_media_probe",
    "verify_artifact",
]
