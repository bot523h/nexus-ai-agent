"""Cooperative, filesystem-local fencing for durable effect publication.

All writers and verifiers must use this adapter on the same POSIX filesystem,
whose flock, atomic rename and fsync semantics are reliable. The root and its
contents must be protected from untrusted mutation; this is not a sandbox or
an authority for arbitrary filesystem writers. No SQLite lock is involved.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.studio.durable_dedupe import ExternalEffectObservation
from nexus_ai_agent.creative.studio.plan_identifiers import validate_plan_identity


class StaleEpochError(RuntimeError):
    """The destination has already fenced this execution epoch."""


class EffectPayloadMismatchError(RuntimeError):
    """An effect identity was previously bound to different bytes."""


class DestinationStateError(RuntimeError):
    """The destination's persisted state is corrupt or inconsistent."""


class FencedFilesystemEffectDestination:
    """Publish one immutable payload per project/effect, with durable tombstones.

    Root provisioning and filesystem access control are deployment concerns.
    Locks are cooperative, not protection from processes bypassing this class.
    """

    def __init__(self, root: Path, *, project_id: str) -> None:
        validate_plan_identity(project_id, name="project_id")
        self._project_id = project_id
        self._root = Path(root)
        if not self._root.is_dir():
            raise ValueError("destination root must be provisioned before use")
        self._project = self._root / hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        self._project.mkdir(exist_ok=True)
        self._sync_dir(self._root)

    @staticmethod
    def _validate(effect_key: str, epoch: int) -> None:
        if not isinstance(effect_key, str) or re.fullmatch(r"[0-9a-f]{64}", effect_key) is None:
            raise ValueError("effect_key must be lowercase SHA-256 hex")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("fencing_epoch must be a positive integer")

    @staticmethod
    def _sync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        fd = os.open(self._project / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._clean_stages(key)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _clean_stages(self, key: str) -> None:
        # Only artifacts created by this adapter for this effect are removed,
        # after acquiring the lock held throughout every cooperating write.
        removed = False
        for kind in ("json", "effect"):
            for path in self._project.glob(f"{key}.{kind}.stage-*"):
                path.unlink()
                removed = True
        if removed:
            self._sync_dir(self._project)

    def _metadata(self, key: str) -> dict[str, Any]:
        path = self._project / (key + ".json")
        if not path.exists():
            return {"highest_epoch": 0, "tombstone_epoch": 0, "payload_digest": None}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or set(data) != {"highest_epoch", "tombstone_epoch", "payload_digest"}
                or type(data["highest_epoch"]) is not int
                or type(data["tombstone_epoch"]) is not int
                or data["highest_epoch"] < data["tombstone_epoch"]
                or data["tombstone_epoch"] < 0
                or (
                    data["payload_digest"] is not None
                    and (
                        not isinstance(data["payload_digest"], str)
                        or re.fullmatch(r"[0-9a-f]{64}", data["payload_digest"]) is None
                    )
                )
            ):
                raise ValueError("invalid metadata")
            return data
        except (OSError, UnicodeError, ValueError) as exc:
            raise DestinationStateError("invalid destination metadata") from exc

    def _atomic_write(self, path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".stage-", dir=self._project)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync_dir(self._project)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _write_metadata(self, key: str, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._atomic_write(self._project / (key + ".json"), encoded)

    def _observation(
        self, key: str, epoch: int, status: str, data: dict[str, Any]
    ) -> ExternalEffectObservation:
        # This digest is a state commitment, not a signature. The separate
        # verifier must re-read trusted destination state under its own lock.
        evidence = json.dumps(
            {
                "project_id": self._project_id,
                "effect_key": key,
                "epoch": epoch,
                "status": status,
                "metadata": data,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(evidence).hexdigest()
        if status == "PRESENT":
            return ExternalEffectObservation(key, epoch, "PRESENT", digest)
        return ExternalEffectObservation(key, epoch, "ABSENT", digest)

    def _published_digest(self, key: str, data: dict[str, Any]) -> str | None:
        path = self._project / (key + ".effect")
        if not path.exists():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if data["payload_digest"] != digest:
            raise DestinationStateError("published bytes do not match immutable payload digest")
        return digest

    def apply(
        self, *, effect_key: str, fencing_epoch: int, payload: bytes
    ) -> ExternalEffectObservation:
        self._validate(effect_key, fencing_epoch)
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        digest = hashlib.sha256(payload).hexdigest()
        with self._locked(effect_key):
            data = self._metadata(effect_key)
            if fencing_epoch <= data["tombstone_epoch"] or fencing_epoch < data["highest_epoch"]:
                raise StaleEpochError("destination has fenced this epoch")
            if data["payload_digest"] not in (None, digest):
                raise EffectPayloadMismatchError("effect key already binds different bytes")
            published = self._published_digest(effect_key, data)
            data["highest_epoch"] = max(data["highest_epoch"], fencing_epoch)
            data["payload_digest"] = digest
            self._write_metadata(effect_key, data)
            if published is None:
                self._atomic_write(self._project / (effect_key + ".effect"), payload)
            return self._observation(effect_key, fencing_epoch, "PRESENT", data)

    def inspect(self, *, effect_key: str, fencing_epoch: int) -> ExternalEffectObservation:
        """Atomically fence an absent effect through the requested epoch."""
        self._validate(effect_key, fencing_epoch)
        with self._locked(effect_key):
            data = self._metadata(effect_key)
            if self._published_digest(effect_key, data) is not None:
                return self._observation(effect_key, fencing_epoch, "PRESENT", data)
            data["highest_epoch"] = max(data["highest_epoch"], fencing_epoch)
            data["tombstone_epoch"] = max(data["tombstone_epoch"], fencing_epoch)
            self._write_metadata(effect_key, data)
            return self._observation(effect_key, fencing_epoch, "ABSENT", data)


class FilesystemObservationVerifier:
    """Re-read state under the destination lock; never trusts a digest alone.

    This verifies a live local state, not an authenticated remote attestation.
    Its separate instance and trusted root must be supplied by composition.
    """

    def __init__(self, root: Path, *, project_id: str) -> None:
        self._reader = FencedFilesystemEffectDestination(root, project_id=project_id)

    def verify(self, observation: ExternalEffectObservation) -> bool:
        if (
            not isinstance(observation, ExternalEffectObservation)
            or observation.status == "UNKNOWN"
        ):
            return False
        reader = self._reader
        with reader._locked(observation.effect_key):
            data = reader._metadata(observation.effect_key)
            published = reader._published_digest(observation.effect_key, data)
            if observation.status == "PRESENT" and published is None:
                return False
            if observation.status == "ABSENT" and (
                published is not None or data["tombstone_epoch"] < observation.fencing_epoch
            ):
                return False
            return observation == reader._observation(
                observation.effect_key, observation.fencing_epoch, observation.status, data
            )
