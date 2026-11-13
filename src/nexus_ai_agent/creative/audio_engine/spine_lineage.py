"""Revision-bound audio publication at the NEXUS Spine boundary.

No second queue, database, passport format or filesystem authority lives here.
The host supplies its *canonical* attempt fence and append-only AssetVersion
writer. A fence is a held critical section, NOT a read-token callback: it must
serialize invalidation and publication until the writer commits. This is the
synchronous engine-side adapter; it does not pretend that a filesystem rename
and an external database transaction are intrinsically atomic.

Stage -> Render -> Verify private bytes -> Hold/Check fence -> Recheck bytes
-> Atomic publish -> Append lineage. Nothing is returned as authoritative until
the host writer has accepted the immutable record and verbatim signed passport.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder
from nexus_ai_agent.creative.audio_engine.models import (
    CanonicalStreamProfile,
    LoudnessProfile,
    canonical_hash,
    canonical_json,
)
from nexus_ai_agent.creative.audio_engine.process import CancelToken, ProcessCancelled
from nexus_ai_agent.creative.audio_engine.verify import (
    ArtifactPassport,
    PassportKeyring,
    build_passport,
    verify_passport,
)
from nexus_ai_agent.creative.audio_engine.workspace import (
    PublishResult,
    RuntimeWorkspace,
    hash_file,
)

# This is the canonical passport, not an independently evolving audio format.
AudioArtifactPassport = ArtifactPassport
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
VERIFICATION_TIER = "SIGNED_CROSS_VERIFIED_PRIVATE_BYTES"


class AudioPublishRefused(RuntimeError):
    """No authoritative artifact/lineage can be emitted for this attempt."""


@dataclass(frozen=True)
class AudioPublishIdentity:
    project_id: str
    parent_revision_id: str
    produced_revision_id: str
    attempt_id: str
    fencing_token: int
    recipe_digest: str
    input_sha256: str

    def __post_init__(self) -> None:
        for value in (
            self.project_id,
            self.parent_revision_id,
            self.produced_revision_id,
            self.attempt_id,
        ):
            if not value.strip() or "\x00" in value:
                raise ValueError("lineage identifiers must be non-empty, NUL-free strings")
        if self.parent_revision_id == self.produced_revision_id:
            raise ValueError("an audio derivation must produce a new revision")
        if (
            not isinstance(self.fencing_token, int)
            or isinstance(self.fencing_token, bool)
            or self.fencing_token < 1
        ):
            raise ValueError("fencing_token must be a positive integer")
        for digest in (self.recipe_digest, self.input_sha256):
            if not _DIGEST.fullmatch(digest):
                raise ValueError("lineage digests must be sha256:<64 lowercase hex>")

    @property
    def binding(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True)
class AudioAssetVersionRecord(AudioPublishIdentity):
    asset_id: str
    output_sha256: str
    passport_digest: str
    measured_lufs: float
    measured_true_peak_dbtp: float
    verification_tier: str = VERIFICATION_TIER

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.asset_id.strip():
            raise ValueError("asset_id must be non-empty")
        if not all(
            _DIGEST.fullmatch(value) for value in (self.output_sha256, self.passport_digest)
        ):
            raise ValueError("output and passport digests must be sha256:<64 lowercase hex>")
        if not all(
            math.isfinite(value) for value in (self.measured_lufs, self.measured_true_peak_dbtp)
        ):
            raise ValueError("lineage requires finite loudness and true-peak measurements")
        if self.verification_tier != VERIFICATION_TIER:
            raise ValueError("the adapter cannot confer an unperformed verification tier")

    @property
    def version_id(self) -> str:
        """Same immutable evidence -> same id; revisions/recipes remain distinct."""
        return "aav_" + canonical_hash(asdict(self)).split(":", 1)[1]


@dataclass(frozen=True)
class AudioStagedPublishReceipt:
    asset_version: AudioAssetVersionRecord
    publication: PublishResult
    # A string, rather than a mutable nested model/dict: stored verbatim.
    passport_json: str


class AudioPublishFence(Protocol):
    def hold(self, identity: AudioPublishIdentity) -> AbstractContextManager[None]:
        """Hold the host's project/attempt fence through publish + lineage commit."""
        ...

    def assert_active(self, identity: AudioPublishIdentity) -> None:
        """Refuse a superseded attempt/revision/token, under the held fence."""
        ...


class AudioAssetVersionWriter(Protocol):
    def record_asset_version(
        self, record: AudioAssetVersionRecord, *, passport_json: str, publication: PublishResult
    ) -> None:
        """Append/idempotently insert via the canonical AssetVersion authority.

        Must commit synchronously or raise before returning; never update an
        ancestor. The passport digest is the uniqueness key, not bytes alone.
        """
        ...


def _check_cancel(cancel: CancelToken | None) -> None:
    if cancel is not None and cancel.cancelled:
        raise ProcessCancelled("audio publication cancelled before the fenced commit")


def execute_fenced_staged_audio_publish(
    *,
    workspace: RuntimeWorkspace,
    source: Path,
    destination: Path,
    identity: AudioPublishIdentity,
    operation: str,
    asset_id: str,
    render: Callable[[Path, Path], Path],
    engine: FfmpegTranscoder,
    fence: AudioPublishFence,
    lineage: AudioAssetVersionWriter,
    signing_key: bytes,
    signer_key_id: str,
    keyring: PassportKeyring,
    profile: CanonicalStreamProfile | None = None,
    loudness_profile: LoudnessProfile | None = None,
    cancel: CancelToken | None = None,
) -> AudioStagedPublishReceipt:
    """Publish only exact, privately verified bytes under an active host fence.

    ``render`` receives an immutable input copy and a unique run directory. It
    must return a regular file inside that directory. The destination must be
    under ``media/`` and is never replaced; immutable version keys are expected.
    A host must not implement ``hold`` as a no-op or invalidate a token outside
    that same lock/transaction. The adapter supplies no permissive default.
    """
    workspace.prepare()
    target = workspace.contain(destination)
    if not target.is_relative_to(workspace.media_dir) or target == workspace.media_dir:
        raise AudioPublishRefused("the publish destination must be inside media/")
    _check_cancel(cancel)
    staged_input = workspace.stage_input(source, expected_sha256=identity.input_sha256)
    if hash_file(staged_input) != identity.input_sha256:
        raise AudioPublishRefused("input bytes changed during private staging")
    with workspace.run_directory("lineage-" + uuid.uuid4().hex) as run_dir:
        produced = workspace.contain(render(staged_input, run_dir))
        if not produced.is_relative_to(run_dir):
            raise AudioPublishRefused("the renderer returned a file outside its private attempt")
        relative = str(produced.relative_to(workspace.root))
        workspace.filesystem.require_regular_file(relative)
        digest, size = workspace.filesystem.hash_file(relative)
        expected = "sha256:" + digest
        if size == 0:
            raise AudioPublishRefused("the private renderer produced an empty artifact")
        if hash_file(staged_input) != identity.input_sha256:
            raise AudioPublishRefused("the renderer modified its immutable input")
        _check_cancel(cancel)
        passport = build_passport(
            operation=operation,
            asset_id=asset_id,
            path=produced,
            source_sha256=identity.input_sha256,
            expected_output_sha256=expected,
            profile=profile or CanonicalStreamProfile(),
            loudness_profile=loudness_profile,
            engine=engine,
            max_analysis_seconds=0.0,
            signing_key=signing_key,
            signer_key_id=signer_key_id,
            transactional_identity=identity.binding,
            cancel=cancel,
        )
        report = verify_passport(
            passport,
            path=produced,
            source=staged_input,
            engine=engine,
            keyring=keyring,
            require_signature=True,
            cross_check=True,
            cancel=cancel,
        )
        if not passport.verified or not report.passed:
            raise AudioPublishRefused(f"private passport verification refused: {report.codes}")
        if (
            passport.transactional_identity != identity.binding
            or passport.source_sha256 != identity.input_sha256
            or passport.output_sha256 != expected
        ):
            raise AudioPublishRefused("passport is not bound to this revision/recipe/byte sequence")
        measurement = passport.loudness
        if measurement is None or measurement.integrated_lufs is None:
            raise AudioPublishRefused("no measured programme loudness for the AssetVersion")
        if measurement.true_peak_dbtp is None:
            raise AudioPublishRefused("no measured true peak for the AssetVersion")
        record = AudioAssetVersionRecord(
            project_id=identity.project_id,
            parent_revision_id=identity.parent_revision_id,
            produced_revision_id=identity.produced_revision_id,
            attempt_id=identity.attempt_id,
            fencing_token=identity.fencing_token,
            recipe_digest=identity.recipe_digest,
            input_sha256=identity.input_sha256,
            asset_id=asset_id,
            output_sha256=expected,
            passport_digest=passport.payload_sha256,
            measured_lufs=measurement.integrated_lufs,
            measured_true_peak_dbtp=measurement.true_peak_dbtp,
        )
        passport_json = canonical_json(passport.model_dump(mode="json"))
        with fence.hold(identity):
            fence.assert_active(identity)
            _check_cancel(cancel)
            # A verifier or fence hook can mutate staging. Re-read the actual
            # private bytes AFTER both have run and BEFORE any public name exists.
            current_digest, current_size = workspace.filesystem.hash_file(relative)
            if ("sha256:" + current_digest, current_size) != (expected, size):
                raise AudioPublishRefused("private staged bytes changed after verification")
            publication = workspace.publish(
                produced, target, replace=False, protected=(staged_input,)
            )
            try:
                if publication.sha256 != record.output_sha256:
                    raise AudioPublishRefused("atomic publication did not preserve verified bytes")
                lineage.record_asset_version(
                    record, passport_json=passport_json, publication=publication
                )
            except BaseException:
                # Only this attempt's immutable, fence-held name is retracted.
                # This is not a distributed transaction: host crash recovery is
                # still owned by the existing queue/AssetVersion authority.
                workspace.filesystem.unlink(str(target.relative_to(workspace.root)))
                raise
        return AudioStagedPublishReceipt(record, publication, passport_json)


__all__ = [
    "AudioArtifactPassport",
    "AudioAssetVersionRecord",
    "AudioAssetVersionWriter",
    "AudioPublishFence",
    "AudioPublishIdentity",
    "AudioPublishRefused",
    "AudioStagedPublishReceipt",
    "VERIFICATION_TIER",
    "execute_fenced_staged_audio_publish",
]
