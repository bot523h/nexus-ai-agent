"""Atomic artifact publication at the job/adapter boundary.

The rule this module enforces at the *job* boundary (the render lane keeps its
own staging inside ``creative/rendering``; nothing here rewrites it)::

    partial artifact  ≠  final artifact        invalid artifact  ≠  successful job

Publication is the single place where staged bytes may become the destination —
and they become it only after the canonical verifier accepted them, by an
atomic rename inside the destination's own filesystem:

1. the caller renders into a **staging path** (never the destination);
2. :func:`publish_artifact` decides what to do with the destination:

   * **no destination** → fsync the staging bytes, ``os.replace`` them onto the
     destination, write the identity sidecar atomically;
   * **destination is this job's own already-valid artifact** (matching identity
     sidecar + it re-verifies) → **reuse it, never overwrite it**: a retry
     preserves a valid artifact and reports its measured evidence;
   * **destination exists but does not verify** (zero-byte, truncated, tampered)
     → quarantine it (``<name>.invalid-<token>``, moved, never deleted) and
     publish the verified bytes;
   * **destination is valid but belongs to another identity** (no sidecar, or a
     different command/revision/operation) → **refuse** with
     :class:`DestinationOccupied`: a valid artifact this job cannot attribute is
     never silently replaced, truncated or unlinked.

Publishing is deliberately *not* a content-addressed store rewrite: the
repository's artifact contract (`workspace/output.mp4` paths handed to the
delivery layer, the Runtime's ``verify_artifact`` identity model) is consumed
unchanged. The sidecar JSON is the durable identity record that makes the
"mine or not mine?" question answerable *without* trusting filenames.

Durability note (honest boundary): the staging file is fsynced before the
rename, and the directory is deliberately **not** fsynced — the file's own flush
orders "bytes durable" before "name visible", and both crash outcomes are safe
(a missing name; or a leftover ``.<name>.part…`` staging entry, which can never
be mistaken for a destination because it is not the destination name). This is
the same trade-off recorded by the reference implementation for artifact
publication (``local-operator#1202``) cited in
``docs/architecture/JOB_LIFECYCLE_CONTRACT.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

#: Suffix of the identity sidecar written next to every published artifact.
SIDECAR_SUFFIX = ".nexus-artifact.json"

#: Marker a document artifact must contain to be considered its declared kind.
DOCUMENT_MARKERS: dict[str, tuple[str, ...]] = {
    "srt": ("-->",),
    "otio": ('"OTIO_SCHEMA"',),
    "text": (),
}


class PublicationError(ValueError):
    """Publication refused (fail-closed). The destination is left as it was."""


class VerificationFailed(PublicationError):
    """The bytes did not verify; they must never be published as the artifact."""


class DestinationOccupied(PublicationError):
    """A valid artifact owned by another identity occupies the destination."""


class StagingNotPublishable(PublicationError):
    """The staging path is unusable (missing, or not on the destination's filesystem)."""


def sha256_file(path: Path) -> str:
    """``sha256:<hex>`` streamed from a file — the physical identity of bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# document artifacts (dependency-free verification; media probing stays with
# the Runtime's canonical ``creative.artifacts.verify_artifact``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentArtifact:
    """A verified document artifact (SRT / OTIO): bytes, size, structure."""

    path: str
    kind: str
    size_bytes: int
    sha256: str
    prover: str

    def evidence(self) -> dict[str, Any]:
        return {
            "artifact_path": self.path,
            "artifact_kind": self.kind,
            "size_bytes": self.size_bytes,
            "physical_artifact_sha256": self.sha256,
            "prover": self.prover,
        }


def verify_document_artifact(
    path: Path,
    *,
    kind: str,
    expected_sha256: str | None = None,
) -> DocumentArtifact:
    """Verify a text/document artifact: exists, non-empty, hashed, well-formed.

    Checks in order — the first failure raises, so a broken document can never
    be published as the job's product:

    1. the path exists and is a non-empty regular file;
    2. the bytes hash from disk (and match ``expected_sha256`` when pinned);
    3. the declared ``kind`` has a marker table (unknown kinds fail closed);
    4. every marker for that kind is present (``srt`` needs a cue arrow,
       ``otio`` the OTIO schema key), so an empty or truncated document is
       caught by structure, not by optimism.
    """
    candidate = Path(path)
    if kind not in DOCUMENT_MARKERS:
        raise VerificationFailed(f"unknown document kind {kind!r}; refusing to verify blindly")
    if not candidate.is_file():
        raise VerificationFailed(f"artifact missing: {candidate}")
    size = candidate.stat().st_size
    if size <= 0:
        raise VerificationFailed(f"artifact is empty: {candidate}")
    measured = sha256_file(candidate)
    if expected_sha256 is not None and measured != expected_sha256:
        raise VerificationFailed(
            "artifact hash mismatch for "
            f"{candidate}: measured {measured} != expected {expected_sha256}"
        )
    text = candidate.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise VerificationFailed(f"artifact has no content: {candidate}")
    for marker in DOCUMENT_MARKERS[kind]:
        if marker not in text:
            raise VerificationFailed(
                f"artifact is not a well-formed {kind} document ({marker!r} missing): {candidate}"
            )
    prover = f"structure:{kind}"
    if kind == "otio":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise VerificationFailed(f"artifact is not valid JSON: {candidate}: {exc}") from exc
        prover = "json:OTIO_SCHEMA"
    return DocumentArtifact(
        path=str(candidate), kind=kind, size_bytes=size, sha256=measured, prover=prover
    )


# ---------------------------------------------------------------------------
# identity sidecar
# ---------------------------------------------------------------------------


def sidecar_path(destination: Path) -> Path:
    return destination.with_name(destination.name + SIDECAR_SUFFIX)


def read_published_identity(destination: Path) -> dict[str, Any] | None:
    """Read the identity sidecar, or ``None`` when absent/unreadable.

    An unreadable sidecar is ``None`` — never a partial match (fail-closed:
    without a trustworthy record the destination cannot be claimed as ours).
    """
    path = sidecar_path(Path(destination))
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_sidecar(
    destination: Path, *, identity: Mapping[str, Any], artifact: Mapping[str, Any]
) -> None:
    path = sidecar_path(destination)
    payload = {"identity": dict(identity), "artifact": dict(artifact)}
    staged = path.with_name(f".{path.name}.{uuid4().hex[:8]}.tmp")
    try:
        with staged.open("w", encoding="utf-8") as handle:
            handle.write(_canonical(payload))
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# publication
# ---------------------------------------------------------------------------

#: Injected verifier: accepts a path or raises (Runtime ``verify_artifact``, the
#: document verifier above, or a test double).
VerifyFn = Callable[[Path], Any]


@dataclass(frozen=True)
class PublishedArtifact:
    """The outcome of a publication attempt (fresh bytes or a preserved original)."""

    destination: str
    sha256: str
    size_bytes: int
    reused: bool
    identity: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_path": self.destination,
            "physical_artifact_sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "reused_existing_artifact": self.reused,
            "identity": dict(self.identity),
        }


def _fsync_file(path: Path) -> None:
    """Best-effort fsync of the staging bytes before the rename."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - staging vanished/permissions
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - filesystems without fsync support
        pass
    finally:
        os.close(descriptor)


def _quarantine(destination: Path) -> Path:
    """Move an invalid destination aside (never deleted) and return its new path."""
    token = uuid4().hex[:8]
    target = destination.with_name(f"{destination.name}.invalid-{token}")
    destination.replace(target)
    return target


def _same_filesystem(staging: Path, destination: Path) -> bool:
    try:
        return staging.stat().st_dev == destination.parent.stat().st_dev
    except OSError:
        return False


def publish_artifact(
    *,
    staging_path: Path,
    destination: Path,
    identity: Mapping[str, Any],
    verify: VerifyFn,
    reuse_verified: bool = True,
) -> PublishedArtifact:
    """Publish verified staging bytes to ``destination`` atomically.

    ``identity`` is the logical owner record (command/operation/revision/….);
    it is compared against the destination's sidecar to answer "is the existing
    artifact this job's own?". ``verify`` is the canonical verifier for the
    artifact's kind — injected so the Runtime module stays the sole authority on
    media probing and this module stays dependency-free.

    Raises :class:`DestinationOccupied` when a *valid* artifact of another
    identity occupies the destination, :class:`VerificationFailed` when the
    staging bytes (or a reused candidate) fail verification, and
    :class:`StagingNotPublishable` when staging is missing or on another
    filesystem.
    """
    staging = Path(staging_path)
    destination = Path(destination)
    if not staging.is_file():
        raise StagingNotPublishable(f"staging artifact missing: {staging}")
    if not destination.parent.is_dir():
        raise StagingNotPublishable(f"destination directory missing: {destination.parent}")
    if not _same_filesystem(staging, destination):
        raise StagingNotPublishable(
            f"staging {staging} is not on the destination filesystem — "
            "publishing would not be atomic"
        )

    identity_record = dict(identity)
    existing_sidecar = read_published_identity(destination)

    if destination.exists():
        owned_by_us = existing_sidecar is not None and _canonical(
            existing_sidecar.get("identity") or {}
        ) == _canonical(identity_record)
        if owned_by_us:
            if reuse_verified:
                try:
                    verify(destination)
                except Exception:
                    _quarantine(destination)
                else:
                    return PublishedArtifact(
                        destination=str(destination),
                        sha256=sha256_file(destination),
                        size_bytes=destination.stat().st_size,
                        reused=True,
                        identity=identity_record,
                    )
            else:  # pragma: no cover - policy switch kept for callers that never reuse
                raise DestinationOccupied(f"{destination} is already published for this identity")
        else:
            # Valid but unattributable bytes are somebody's artifact: refuse.
            try:
                verify(destination)
            except Exception:
                _quarantine(destination)
            else:
                raise DestinationOccupied(
                    f"{destination} holds a valid artifact that this job cannot claim "
                    "(no matching identity sidecar) — refusing to replace it"
                )

    # Defence in depth: publication is the only place staged bytes may become
    # the destination, so it refuses to promote bytes its own verifier cannot
    # accept — a caller bug cannot turn an unverified file into an artifact.
    try:
        verify(staging)
    except Exception as exc:
        raise VerificationFailed(f"staging artifact did not verify: {staging}: {exc}") from exc

    _fsync_file(staging)
    staging.replace(destination)
    published = PublishedArtifact(
        destination=str(destination),
        sha256=sha256_file(destination),
        size_bytes=destination.stat().st_size,
        reused=False,
        identity=identity_record,
    )
    _write_sidecar(
        destination,
        identity=identity_record,
        artifact={
            "physical_artifact_sha256": published.sha256,
            "size_bytes": published.size_bytes,
        },
    )
    return published


__all__ = [
    "DOCUMENT_MARKERS",
    "SIDECAR_SUFFIX",
    "DestinationOccupied",
    "DocumentArtifact",
    "PublicationError",
    "PublishedArtifact",
    "StagingNotPublishable",
    "VerificationFailed",
    "VerifyFn",
    "publish_artifact",
    "read_published_identity",
    "sha256_file",
    "sidecar_path",
    "verify_document_artifact",
]
