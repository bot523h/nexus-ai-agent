"""Zero-dependency BYOS drivers and their shared storage protocol.

Backends are atomic local storage, SigV4-compatible S3 (R2/MinIO included), and a
verified single-file ``.nagarvault`` bundle for offline transfer. Each supports
quota controls and fail-closed lease ownership; S3 transport is injectable for tests.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import struct
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
    ENVELOPE_MAGIC,
    VaultSnapshotManifest,
)
from nexus_ai_agent.storage.personal_cloud_vault.errors import (
    PersonalVaultError,
    VaultAuthenticationError,
    VaultQuotaExceededError,
    VaultRevisionConflictError,
    VaultTamperDetectedError,
    VaultTransportError,
)


class VaultDriverError(VaultTransportError):
    """Typed fail-closed driver error for missing or unverifiable lease authority."""

    __slots__ = ()


def _load_fcntl() -> Any:
    """Import ``fcntl`` when the platform provides it (POSIX) and ``None`` otherwise."""
    try:  # pragma: no cover - platform dependent import
        import fcntl as posix_fcntl
    except ImportError:  # pragma: no cover - non-POSIX platforms (Windows)
        return None
    return posix_fcntl


#: POSIX ``fcntl`` module when available (``None`` on Windows); kernel-backed locking.
_FCNTL: Any = _load_fcntl()

_CHUNK_ID_RE = re.compile(r"^[0-9a-f]{16,128}$")
_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GC_COORD_LOCK_FILENAME = ".gc_coord.lock"
_GC_COORD_OWNER_FILENAME = ".gc_coord.owner"
_LEASE_DIR_FILENAME_RE = re.compile(r"^lease-[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")


def _validate_chunk_id(chunk_id: str) -> str:
    if not _CHUNK_ID_RE.fullmatch(chunk_id):
        raise PersonalVaultError(f"Invalid chunk_id format: {chunk_id!r}")
    return chunk_id


def _validate_snapshot_id(snapshot_id: str) -> str:
    if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id) or ".." in snapshot_id:
        raise PersonalVaultError(f"Invalid snapshot_id format: {snapshot_id!r}")
    return snapshot_id


def _read_lease_file_once(path: Path) -> bytes | None:
    """Read a lease through one no-follow descriptor and prove its inode stayed stable."""
    required_flags = ("O_RDONLY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, flag) for flag in required_flags):
        raise VaultTransportError("Platform cannot safely open local lease files")
    if _FCNTL is not None and not hasattr(os, "O_NONBLOCK"):
        raise VaultTransportError(
            "POSIX platform cannot safely open local lease files without O_NONBLOCK"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise VaultTransportError(f"Cannot open push lease {path}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise VaultTransportError(f"Push lease is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise VaultRevisionConflictError(
                f"Push lease {path} changed while it was being read"
            ) from exc
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise VaultRevisionConflictError(f"Push lease {path} changed while it was being read")
        return b"".join(chunks)
    except OSError as exc:
        raise VaultTransportError(f"Cannot read push lease {path}: {exc}") from exc
    finally:
        os.close(fd)


def _validate_positive_finite_seconds(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PersonalVaultError(f"{name} must be a finite positive number, got {value!r}")
    as_float = float(value)
    if not math.isfinite(as_float) or as_float <= 0.0:
        raise PersonalVaultError(f"{name} must be finite and positive, got {value!r}")
    return as_float


def _validate_finite_epoch(value: float | None, *, default_now: float) -> float:
    if value is None:
        return default_now
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PersonalVaultError(f"now_epoch must be a finite number, got {value!r}")
    as_float = float(value)
    if not math.isfinite(as_float):
        raise PersonalVaultError(f"now_epoch must be finite, got {value!r}")
    return as_float


def _compute_lease_token_verifier(project_id: str, lease_id: str, lease_token: str) -> str:
    """Domain-separated, one-way verifier stored in shared lease documents."""
    fields = (project_id.encode("utf-8"), lease_id.encode("utf-8"), lease_token.encode("utf-8"))
    material = bytearray(b"nexus-lease-token-verifier-v1\x00")
    for field_bytes in fields:
        material.extend(len(field_bytes).to_bytes(8, "big"))
        material.extend(field_bytes)
    return hashlib.sha256(material).hexdigest()


def _lease_owner_fields(raw: bytes) -> tuple[str, str, str]:
    """Return ``(lease_id, project_id, token_verifier)``; malformed docs fail closed."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "", "", ""
    if not isinstance(payload, dict):
        return "", "", ""
    lease_id = payload.get("lease_id")
    project_id = payload.get("project_id")
    verifier = payload.get("lease_token_verifier")
    return (
        lease_id if isinstance(lease_id, str) else "",
        project_id if isinstance(project_id, str) else "",
        verifier if isinstance(verifier, str) else "",
    )


def _lease_record_matches_owner(
    raw: bytes,
    *,
    project_id: str,
    lease_id: str,
    lease_token: str,
) -> bool:
    actual_id, actual_project, actual_verifier = _lease_owner_fields(raw)
    expected_verifier = _compute_lease_token_verifier(project_id, lease_id, lease_token)
    return (
        actual_id == lease_id
        and actual_project == project_id
        and bool(actual_verifier)
        and hmac.compare_digest(expected_verifier, actual_verifier)
    )


def _owned_lease_pair(
    owned_tokens: dict[str, str],
    owned_etags: dict[str, str],
    lease_id: str,
    expected_etag: str | None,
) -> tuple[str, str] | None:
    """Return the instance's exact credential pair, or ``None`` for a peer/stale call."""
    if expected_etag is not None and (
        not isinstance(expected_etag, str) or not expected_etag.strip()
    ):
        raise VaultTransportError("Lease update/release requires a non-empty expected ETag")
    token = owned_tokens.get(lease_id)
    etag = owned_etags.get(lease_id)
    if not token or not etag:
        return None
    if expected_etag is not None and expected_etag != etag:
        return None
    return token, etag


def _build_lease_json_bytes(
    *,
    lease_id: str,
    project_id: str,
    now_epoch: float,
    ttl_seconds: float,
    chunk_ids: Iterable[str],
    lease_token: str,
) -> bytes:
    validated_chunks = sorted({_validate_chunk_id(cid) for cid in chunk_ids})
    # ``ttl_seconds`` is validated finite/positive by every caller, so the computed
    # expiration can never become non-finite (float("inf") is not reachable by rounding).
    expires_at = now_epoch + ttl_seconds
    return json.dumps(
        {
            "lease_id": lease_id,
            "pid": os.getpid(),
            "project_id": project_id,
            # Store only a domain-separated verifier: the raw owner secret never
            # enters the shared filesystem, bundle, or object store.
            "lease_token_verifier": _compute_lease_token_verifier(
                project_id, lease_id, lease_token
            ),
            "created_at_epoch": now_epoch,
            "expires_at_epoch": expires_at,
            "ttl_seconds": ttl_seconds,
            "chunk_ids": validated_chunks,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


#: Lifetime of a GC fence object (seconds).
#:
#: A fence is a *claim held by a running collector*, not a tombstone: it carries an
#: absolute expiry so that a collector which dies mid-pass cannot pin a chunk for
#: ever, and so that a writer can tell a live claim from the debris of a crashed
#: one. The TTL is generous compared with a single chunk delete (seconds) because
#: the cost of reclaiming a *live* fence too early is unbounded (a chunk deleted
#: under a push), while the cost of reclaiming a dead one too late is a retry.
GC_FENCE_TTL_SECONDS: float = 300.0

#: Leeway (seconds) applied on the *dead* side of every distributed expiry decision.
#:
#: Two hosts sharing one bucket do not share a clock. A writer whose clock lags by a
#: few minutes computes an earlier ``expires_at_epoch`` than the collector's ``now``,
#: and without a leeway the collector would reap a lease that is seconds old — then
#: delete the chunk it protects. The margin is added to *every* expiry before it is
#: believed, which is the conservative direction: a lease or fence is honoured for up
#: to this long after it nominally expired, and only a claim that is provably older
#: than ``deadline + margin`` is ever treated as debris. Combined with the bucket's
#: own ``Last-Modified``/``Date`` anchor (see :func:`_lease_deadline_epoch`) this
#: covers skew in both directions: a lagging writer cannot make its own fresh lease
#: look expired, and a leading writer cannot make its lease look older than it is.
CLOCK_SKEW_SAFETY_MARGIN_SECONDS: float = 300.0

#: The exact prefix an AEAD wire envelope starts with (``>H`` length + magic).
_WIRE_ENVELOPE_HEADER_BYTES = 2 + len(ENVELOPE_MAGIC) + 2 + 32


def _wire_envelope_chunk_id(raw: bytes) -> str | None:
    """Return the chunk id embedded in an AEAD wire envelope, or ``None``.

    The envelope's authenticated header binds the chunk id (see
    ``crypto_chunk_envelope._build_envelope_header``), so a collector that staged
    these bytes can prove they belong to the object key it is restoring before it
    writes a single byte back. Payloads that are not envelopes (opaque fixtures,
    plaintext tests) return ``None`` and are verified by read-back instead — the
    driver must not assume another layer's on-disk format.
    """
    if len(raw) < _WIRE_ENVELOPE_HEADER_BYTES:
        return None
    header_len = int.from_bytes(raw[0:2], "big")
    if header_len < len(ENVELOPE_MAGIC) + 2 + 32:
        return None
    if raw[2 : 2 + len(ENVELOPE_MAGIC)] != ENVELOPE_MAGIC:
        return None
    return raw[2 + len(ENVELOPE_MAGIC) + 2 : _WIRE_ENVELOPE_HEADER_BYTES].hex()


def _build_gc_fence_body(
    token: str,
    chunk_id: str,
    *,
    now_epoch: float,
    ttl_seconds: float = GC_FENCE_TTL_SECONDS,
) -> bytes:
    """Serialize a fence claim: owner token, subject chunk, creation, and expiry."""
    return json.dumps(
        {
            "fence_token": token,
            "chunk_id": chunk_id,
            "created_at_epoch": now_epoch,
            "expires_at_epoch": now_epoch + ttl_seconds,
            # Carried explicitly so a *reader* on another host can re-derive the
            # deadline from the bucket's own clock instead of trusting ours.
            "ttl_seconds": ttl_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _GcFence:
    """A GC fence object as observed on the bucket."""

    raw: bytes
    token: str
    expires_at_epoch: float | None
    etag: str | None
    #: ``ttl_seconds`` from the body, when it parses, and the bucket's own clock at
    #: the moment of observation. Both are optional because an older collector may
    #: have written a fence without them, and because a transport may not report
    #: ``Last-Modified``; in that case only ``expires_at_epoch`` is available.
    ttl_seconds: float | None = None
    observed_at_epoch: float | None = None

    def deadline_epoch(self) -> float | None:
        """Latest instant the claim may still be honoured, or ``None`` if unknown.

        Two independent estimates are combined, and the *later* one wins:

        * ``expires_at_epoch`` — written by the collector's clock;
        * ``observed_at_epoch + ttl_seconds`` — the same lifetime measured from the
          bucket's clock (``Last-Modified``/``Date``), which is immune to the
          collector's clock being wrong. A collector whose clock lags writes an
          ``expires_at_epoch`` in the past while its claim is seconds old; the server
          anchor keeps that claim alive for its full TTL.
        """
        candidates: list[float] = []
        if self.expires_at_epoch is not None:
            candidates.append(self.expires_at_epoch)
        if self.ttl_seconds is not None and self.observed_at_epoch is not None:
            candidates.append(self.observed_at_epoch + self.ttl_seconds)
        if not candidates:
            return None
        return max(candidates)

    def is_live(self, now_epoch: float, *, margin_seconds: float = 0.0) -> bool:
        """True unless the claim has *provably* expired, leeway included.

        An unparsable fence, or one with no usable expiry, counts as live: a fence
        we cannot read is not evidence that no collector is running. ``margin_seconds``
        widens the live side, because the two clocks that matter here are on different
        machines (see :data:`CLOCK_SKEW_SAFETY_MARGIN_SECONDS`).
        """
        deadline = self.deadline_epoch()
        if deadline is None:
            return True
        return deadline + margin_seconds > now_epoch


def _parse_gc_fence(
    raw: bytes,
    etag: str | None,
    *,
    observed_at_epoch: float | None = None,
) -> _GcFence:
    """Parse a fence body without ever raising (untrusted input, fail-closed)."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _GcFence(raw=raw, token="", expires_at_epoch=None, etag=etag)
    if not isinstance(parsed, dict):
        return _GcFence(raw=raw, token="", expires_at_epoch=None, etag=etag)
    token = parsed.get("fence_token")
    raw_expiry = parsed.get("expires_at_epoch")
    expiry: float | None = None
    if isinstance(raw_expiry, (int, float)) and not isinstance(raw_expiry, bool):
        with contextlib.suppress(OverflowError, ValueError):
            candidate = float(raw_expiry)
            if math.isfinite(candidate):
                expiry = candidate
    raw_ttl = parsed.get("ttl_seconds")
    ttl: float | None = None
    if isinstance(raw_ttl, (int, float)) and not isinstance(raw_ttl, bool):
        with contextlib.suppress(OverflowError, ValueError):
            candidate_ttl = float(raw_ttl)
            if math.isfinite(candidate_ttl) and candidate_ttl > 0.0:
                ttl = candidate_ttl
    return _GcFence(
        raw=raw,
        token=token if isinstance(token, str) else "",
        expires_at_epoch=expiry,
        etag=etag,
        ttl_seconds=ttl,
        observed_at_epoch=observed_at_epoch,
    )


def _parse_and_validate_lease_payload(
    raw: bytes,
    *,
    source_label: str,
) -> tuple[float, set[str]]:
    """Parse a lease document fail-closed, raising :class:`VaultTransportError` if corrupt."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise VaultTransportError(f"Corrupted push lease JSON in {source_label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise VaultTransportError(
            f"Malformed push lease payload in {source_label}: expected JSON object"
        )
    raw_exp = parsed.get("expires_at_epoch")
    if isinstance(raw_exp, bool) or not isinstance(raw_exp, (int, float)):
        raise VaultTransportError(
            f"Missing or non-numeric expires_at_epoch in push lease {source_label}"
        )
    try:
        expires_at = float(raw_exp)
    except OverflowError as exc:  # huge JSON integers e.g. 1e1000 / 10**1000
        raise VaultTransportError(
            f"Unrepresentable expires_at_epoch in push lease {source_label}: {exc}"
        ) from exc
    if not math.isfinite(expires_at):
        raise VaultTransportError(
            f"Non-finite expires_at_epoch in push lease {source_label}: {expires_at!r}"
        )
    raw_ids = parsed.get("chunk_ids")
    if not isinstance(raw_ids, list):
        raise VaultTransportError(f"Malformed chunk_ids in push lease {source_label}: not a list")
    chunk_set: set[str] = set()
    for cid in raw_ids:
        if not isinstance(cid, str) or not _CHUNK_ID_RE.fullmatch(cid):
            raise VaultTransportError(f"Invalid chunk_id {cid!r} in push lease {source_label}")
        chunk_set.add(cid)
    return expires_at, chunk_set


def _lease_verifier_for_recovery(
    raw: bytes,
    *,
    project_id: str,
    lease_id: str,
    source_label: str,
) -> str:
    """Validate a stored lease and return its non-secret token verifier."""
    _parse_and_validate_lease_payload(raw, source_label=source_label)
    actual_id, actual_project, verifier = _lease_owner_fields(raw)
    if actual_id != lease_id or actual_project != project_id:
        raise VaultDriverError(
            f"Lease {lease_id!r} does not match the requested owner at {source_label}"
        )
    if not verifier:
        raise VaultDriverError(f"Lease {lease_id!r} has no recoverable owner verifier")
    return verifier


def _parse_http_or_iso_timestamp(raw_ts: str) -> float | None:
    """Parse RFC 7231 HTTP ``Last-Modified`` or ISO-8601 S3 ``<LastModified>`` string."""
    cleaned = raw_ts.strip()
    if not cleaned:
        return None
    with contextlib.suppress(TypeError, ValueError):
        iso_norm = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
        dt_iso = datetime.fromisoformat(iso_norm)
        if dt_iso.tzinfo is None:
            dt_iso = dt_iso.replace(tzinfo=timezone.utc)
        return dt_iso.timestamp()
    with contextlib.suppress(TypeError, ValueError):
        import email.utils

        dt_http = email.utils.parsedate_to_datetime(cleaned)
        if dt_http.tzinfo is None:
            dt_http = dt_http.replace(tzinfo=timezone.utc)
        return dt_http.timestamp()
    return None


def _server_object_epoch(headers: Mapping[str, str] | None) -> float | None:
    """The bucket's own clock for one object observation, or ``None``.

    ``Last-Modified`` is the authoritative "when did this object come into being"
    stamp and is what makes a lease's lifetime independent of the writer's clock;
    ``Date`` is the fallback for backends that omit it on ``HEAD``/``GET``. Both are
    only ever used to *extend* a deadline, never to shorten one.
    """
    if not headers:
        return None
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    for name in ("last-modified", "date"):
        raw = lowered.get(name)
        if raw is None:
            continue
        parsed = _parse_http_or_iso_timestamp(raw)
        if parsed is not None:
            return parsed
    return None


def _lease_ttl_seconds(raw: bytes) -> float | None:
    """Leniently read ``ttl_seconds`` out of a lease document (``None`` if unusable)."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    raw_ttl = parsed.get("ttl_seconds")
    if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, (int, float)):
        return None
    try:
        ttl = float(raw_ttl)
    except OverflowError:
        return None
    if not math.isfinite(ttl) or ttl <= 0.0:
        return None
    return ttl


def _lease_deadline_epoch(
    raw: bytes,
    headers: Mapping[str, str] | None,
    expires_at_epoch: float,
) -> float:
    """Latest instant a lease may still be honoured, robust to the writer's clock.

    ``expires_at_epoch`` is computed by the *writer*; a writer whose clock lags two
    hours writes a deadline two hours in the collector's past and its brand-new lease
    would be reaped instantly. The bucket's ``Last-Modified`` plus the lease's own
    ``ttl_seconds`` re-derives the same lifetime on a clock neither side controls, and
    the later of the two estimates wins — the conservative direction for a lease.
    """
    deadline = expires_at_epoch
    ttl = _lease_ttl_seconds(raw)
    observed = _server_object_epoch(headers)
    if ttl is not None and observed is not None:
        deadline = max(deadline, observed + ttl)
    return deadline


#: Object-metadata header that carries a unique generation stamp per chunk upload.
#:
#: Chunk ids are content-addressed, so the *ETag* of a chunk is a function of its
#: bytes and nothing else: delete the object and re-upload the same bytes and the
#: ETag comes back identical. A lease that compares ETags alone therefore cannot see
#: the difference between "the chunk I preflighted" and "a different object with the
#: same contents", which is exactly the ABA replacement a collector can produce. Every
#: upload therefore stamps its own generation, so identity is
#: ``(version-id, generation stamp, ETag)`` rather than a content hash.
CHUNK_GENERATION_META_HEADER = "x-amz-meta-nexus-chunk-gen"

#: Returned by :meth:`S3CompatibleSigV4Driver._chunk_generation` when the bucket
#: reports neither a version id nor our generation stamp. It is deliberately not
#: ``""``: a caller must be able to distinguish "unknown identity" from "no ETag",
#: and both are fail-closed.
UNKNOWN_CHUNK_GENERATION = "UNKNOWN_GENERATION"


def _new_chunk_generation() -> str:
    """Mint the generation stamp for one chunk upload (metadata header value)."""
    return uuid.uuid4().hex


def _parse_lease_token_verifier(raw: bytes) -> str:
    """Return the stored one-way owner verifier, or ``""`` if absent/unparsable."""
    _lease_id, _project_id, verifier = _lease_owner_fields(raw)
    return verifier


@dataclass(frozen=True, slots=True)
class _LeaseObjectState:
    """One observation of a lease object: its identity and its absolute deadline.

    ``etag`` is the object version the backend reports; ``lease_token_verifier`` is
    the non-secret verifier carried in the body. Both must match for a rollback/reap.
    """

    raw: bytes
    etag: str
    lease_token_verifier: str
    deadline_epoch: float | None

    def is_ours(self, *, etag: str, lease_token_verifier: str) -> bool:
        """True only when this observation is the version and verifier we expect."""
        if not etag or not self.etag or self.etag != etag:
            return False
        if not lease_token_verifier or not self.lease_token_verifier:
            return False
        return hmac.compare_digest(self.lease_token_verifier, lease_token_verifier)


def _lease_state_matches_token(
    state: _LeaseObjectState,
    *,
    etag: str,
    project_id: str,
    lease_id: str,
    lease_token: str,
) -> bool:
    """Match an in-memory secret token to the shared verifier and exact object version."""
    actual_id, actual_project, _verifier = _lease_owner_fields(state.raw)
    if actual_id != lease_id or actual_project != project_id:
        return False
    expected_verifier = _compute_lease_token_verifier(project_id, lease_id, lease_token)
    return state.is_ours(etag=etag, lease_token_verifier=expected_verifier)


def _pid_is_alive(pid: int) -> bool:
    """Return ``True`` when ``pid`` still runs on this host (POSIX ``kill(pid, 0)``).

    ``PermissionError`` (a live process owned by another user) is treated as alive and
    any unexpected ``OSError`` is treated as alive too, so a false negative can never
    let a competitor break a live owner's lock.
    """
    if pid <= 0 or pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _pid_is_definitely_dead(pid: int) -> bool:
    """Return ``True`` only when the kernel positively reports ``ESRCH`` for ``pid``."""
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _break_stale_ownership_lock(
    owner_path: Path,
    *,
    stale_lock_seconds: float,
) -> bool:
    """Break a stale ``O_EXCL`` ownership file only when its owner is provably dead.

    Never breaks a lock whose recorded PID is still alive on this host, whose payload
    is unparseable, or whose inode changed between inspection and ``unlink``.
    """
    try:
        st = owner_path.lstat()
        raw_owner = owner_path.read_bytes()
        if (time.time() - st.st_mtime) <= stale_lock_seconds:
            return False
        parsed = json.loads(raw_owner.decode("utf-8"))
        owner_pid = int(parsed["pid"])
        if not _pid_is_definitely_dead(owner_pid):
            return False
        if owner_path.lstat().st_ino != st.st_ino:
            return False
        owner_path.unlink()
    except OSError:
        return False
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        return False
    return True


def _release_ownership_lock(
    owner_path: Path,
    *,
    owner_token: str,
    owner_ino: int,
) -> None:
    """Unlink the fallback ownership file only if it is still provably ours.

    A replaced inode or a different ``owner_token`` means the file now belongs to a
    newer holder, so this (possibly slow) release becomes a strict no-op.
    """
    try:
        if owner_path.lstat().st_ino != owner_ino:
            return
        parsed = json.loads(owner_path.read_bytes().decode("utf-8"))
        if str(parsed["owner_token"]) != owner_token:
            return
        if owner_path.lstat().st_ino != owner_ino:
            return
        owner_path.unlink()
    except OSError:
        return
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        return


@contextlib.contextmanager
def _cross_process_file_lock(
    lock_path: Path,
    *,
    owner_path: Path | None = None,
    timeout_seconds: float = 10.0,
    stale_lock_seconds: float = 30.0,
) -> Iterator[None]:
    """Ownership-safe cross-process file mutex used by every local/bundle driver.

    Two complementary layers, both of which never steal a live lock:

    1. **POSIX kernel lock** — ``fcntl.flock(LOCK_EX | LOCK_NB)`` on the persistent
       ``lock_path`` file. The kernel owns the lock, so it is released automatically
       when the holder dies; no staleness heuristic and no ``unlink`` is involved.
    2. **Fallback ownership file** — where ``fcntl`` is unavailable, an
       ``O_CREAT | O_EXCL`` ``owner_path`` records ``{pid, owner_token, created_at}``.
       A competitor only breaks it when the recorded PID is provably gone (and
       re-validates the inode before unlinking). Release re-reads the file and unlinks
       it *only* when both ``st_ino`` and ``owner_token`` still belong to this holder,
       so a slow owner can never delete a newer owner's lock.
    """
    deadline = time.monotonic() + timeout_seconds
    effective_owner_path = owner_path if owner_path is not None else lock_path
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise VaultTransportError(
            f"Failed acquiring vault coordination lock {lock_path}: {exc}"
        ) from exc

    kernel_fd: int | None = None
    owner_ino: int | None = None
    owner_token = f"{os.getpid()}:{uuid.uuid4().hex}"

    try:
        if _FCNTL is not None:
            while True:
                try:
                    _FCNTL.flock(fd, _FCNTL.LOCK_EX | _FCNTL.LOCK_NB)
                    kernel_fd = fd
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise VaultTransportError(
                            f"Timed out acquiring vault coordination lock: {lock_path}"
                        ) from None
                    time.sleep(0.002)
                except OSError as exc:
                    raise VaultTransportError(
                        f"Failed acquiring vault coordination lock {lock_path}: {exc}"
                    ) from exc
        else:
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "owner_token": owner_token,
                    "created_at_epoch": time.time(),
                },
                sort_keys=True,
            ).encode("utf-8")
            while True:
                try:
                    owner_fd = os.open(
                        effective_owner_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                    try:
                        os.write(owner_fd, payload)
                        os.fsync(owner_fd)
                    finally:
                        os.close(owner_fd)
                    owner_ino = effective_owner_path.lstat().st_ino
                    break
                except FileExistsError:
                    if _break_stale_ownership_lock(
                        effective_owner_path,
                        stale_lock_seconds=stale_lock_seconds,
                    ):
                        continue
                    if time.monotonic() >= deadline:
                        raise VaultTransportError(
                            f"Timed out acquiring vault coordination lock: {effective_owner_path}"
                        ) from None
                    time.sleep(0.002)
                except OSError as exc:
                    raise VaultTransportError(
                        f"Failed acquiring vault coordination lock {effective_owner_path}: {exc}"
                    ) from exc

        try:
            yield
        finally:
            if kernel_fd is not None:
                with contextlib.suppress(OSError):
                    _FCNTL.flock(kernel_fd, _FCNTL.LOCK_UN)
            if owner_ino is not None:
                with contextlib.suppress(OSError):
                    _release_ownership_lock(
                        effective_owner_path,
                        owner_token=owner_token,
                        owner_ino=owner_ino,
                    )
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _staging_journal_digest(project_id: str, parent_snapshot_id: str | None) -> str:
    """Return the deterministic staging-journal name digest for a push target."""
    key_bytes = f"{project_id}\x00{parent_snapshot_id or ''}".encode()
    return hashlib.sha256(key_bytes).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class _ArchiveLayout:
    """Streamable pieces of a ``.nagarvault`` archive (no concatenation needed)."""

    header_prefix: bytes
    payload_parts: tuple[bytes, ...]
    index_bytes: bytes

    @property
    def total_size(self) -> int:
        return (
            _BUNDLE_HEADER_TOTAL
            + sum(len(part) for part in self.payload_parts)
            + len(self.index_bytes)
        )

    def iter_segments(self) -> Iterator[bytes | memoryview]:
        """Yield ``header_prefix | archive_sha | payload parts | index`` in order."""

        def _generate() -> Iterator[bytes | memoryview]:
            yield self.header_prefix
            yield _compute_archive_sha(self.header_prefix, self.payload_parts, self.index_bytes)
            for part in self.payload_parts:
                yield memoryview(part).cast("B")
            yield self.index_bytes

        return _generate()

    def to_bytes(self) -> bytes:
        """Materialise the whole archive as ``bytes`` (compatibility helper only).

        Peak memory is ``2 x total_size`` for the duration of this call: one
        ``bytearray`` is filled in place (the archive SHA is still computed
        incrementally, never as ``header + payload + index``), and ``bytes(buffer)``
        then produces the immutable return value. That is unavoidable for any API that
        returns ``bytes``.

        **The disk path does not pay this cost**: :meth:`iter_segments` is what
        ``_atomic_write_stream`` consumes, so persisting a bundle streams the archive
        to the temp file with no archive-sized buffer at all. Use ``iter_segments``
        whenever you do not strictly need a single ``bytes`` object.
        """
        buffer = bytearray(self.total_size)
        cursor = 0
        for segment in self.iter_segments():
            segment_view = memoryview(segment)
            try:
                buffer[cursor : cursor + len(segment_view)] = segment_view
                cursor += len(segment_view)
            finally:
                segment_view.release()
        return bytes(buffer)


#: Fixed read block used by the streaming bundle verifier (never grows with the file).
_BUNDLE_VERIFY_BLOCK_BYTES = 64 * 1024


class _StreamingBinaryReader(Protocol):
    """Seekable binary reader with ``readinto`` (what the streaming verifier needs)."""

    __slots__ = ()

    def read(self, size: int = -1, /) -> bytes: ...

    def readinto(self, buffer: Any, /) -> int: ...

    def seek(self, offset: int, whence: int = 0, /) -> int: ...

    def fileno(self) -> int: ...


def _read_exact(handle: _StreamingBinaryReader, length: int, *, what: str) -> bytes:
    """Read exactly ``length`` bytes from ``handle`` or fail closed."""
    data = handle.read(length)
    if len(data) != length:
        raise VaultTamperDetectedError(f"Truncated .nagarvault {what}")
    return data


def _compute_archive_sha(
    header_prefix: bytes,
    payload_parts: tuple[bytes, ...],
    index_bytes: bytes,
) -> bytes:
    """Hash ``header_prefix || payload parts || index`` incrementally.

    Concatenating the three regions first would allocate a second copy of the whole
    archive just to hash it; feeding the hasher region by region does not.
    """
    hasher = hashlib.sha256()
    hasher.update(header_prefix)
    for part in payload_parts:
        hasher.update(part)
    hasher.update(index_bytes)
    return hasher.digest()


def _atomic_write_file(target_path: Path, data: bytes | bytearray | memoryview) -> None:
    """Atomically persist ``data`` to ``target_path`` via ``.tmp`` + ``fsync`` + ``replace``."""
    _atomic_write_stream(target_path, (data,))


def _atomic_write_stream(
    target_path: Path,
    segments: Iterable[bytes | bytearray | memoryview],
) -> None:
    """Atomically persist ``segments`` in order without joining them in memory.

    Each segment is written straight to the temp file, so the caller can stream a
    whole archive whose total size far exceeds the RAM it is willing to spend.
    """
    parent = target_path.parent
    tmp_name = f".{target_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    tmp_path = parent / tmp_name
    try:
        with open(tmp_path, "wb") as handle:
            for segment in segments:
                handle.write(segment)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target_path)
        dir_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise VaultTransportError(f"Atomic file write failed for {target_path}: {exc}") from exc


@runtime_checkable
class BYOSStorageDriver(Protocol):
    """Protocol implemented by all user-owned Personal Cloud & Local Vault backends."""

    __slots__ = ()

    def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
        """Persist an encrypted chunk envelope under ``chunk_id``."""

    def get_chunk(self, chunk_id: str) -> bytes:
        """Retrieve raw encrypted chunk bytes for ``chunk_id``."""

    def has_chunk(self, chunk_id: str) -> bool:
        """Return ``True`` when ``chunk_id`` already exists on the target store."""

    def put_manifest(self, snapshot_id: str, data: bytes) -> None:
        """Persist raw manifest bytes for ``snapshot_id``."""

    def get_manifest(self, snapshot_id: str) -> bytes:
        """Retrieve raw manifest bytes for ``snapshot_id``."""

    def list_manifests(self) -> list[str]:
        """Return sorted ``snapshot_id`` identifiers stored on the backend."""

    def delete_chunk(self, chunk_id: str) -> None:
        """Delete an unreferenced encrypted chunk identified by ``chunk_id``."""


@dataclass(frozen=True, slots=True)
class VaultStagingJournal:
    """Crash-recovery staging journal tracking non-secret KDF params and uploaded chunks."""

    project_id: str
    parent_snapshot_id: str | None
    salt_hex: str
    kdf_iterations: int
    key_id_hex: str
    chunk_size: int
    updated_at_epoch: float
    uploaded_chunk_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": self.project_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "salt_hex": self.salt_hex,
            "kdf_iterations": self.kdf_iterations,
            "key_id_hex": self.key_id_hex,
            "chunk_size": self.chunk_size,
            "updated_at_epoch": self.updated_at_epoch,
            "uploaded_chunk_ids": list(self.uploaded_chunk_ids),
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> VaultStagingJournal:
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
            raise PersonalVaultError("Unsupported staging journal schema")
        raw_parent = parsed.get("parent_snapshot_id")
        parent_id = None if raw_parent is None else str(raw_parent)
        raw_chunks = parsed["uploaded_chunk_ids"]
        if not isinstance(raw_chunks, list):
            raise PersonalVaultError("Staging journal uploaded_chunk_ids must be a list")
        validated_chunks = tuple(_validate_chunk_id(str(cid)) for cid in raw_chunks)
        salt_hex = str(parsed["salt_hex"])
        key_id_hex = str(parsed["key_id_hex"])
        bytes.fromhex(salt_hex)
        bytes.fromhex(key_id_hex)
        return cls(
            project_id=str(parsed["project_id"]),
            parent_snapshot_id=parent_id,
            salt_hex=salt_hex,
            kdf_iterations=int(parsed["kdf_iterations"]),
            key_id_hex=key_id_hex,
            chunk_size=int(parsed["chunk_size"]),
            updated_at_epoch=float(parsed["updated_at_epoch"]),
            uploaded_chunk_ids=validated_chunks,
        )


# --------------------------------------------------------------------------- #
# 1. LocalDeviceVaultDriver
# --------------------------------------------------------------------------- #


class LocalDeviceVaultDriver:
    """Atomic local-folder / external-drive BYOS vault driver with quota enforcement."""

    __slots__ = (
        "_root_dir",
        "_chunks_dir",
        "_manifests_dir",
        "_leases_dir",
        "_staging_dir",
        "_max_quota_bytes",
        "_owned_lease_etags",
        "_owned_lease_tokens",
    )

    def __init__(
        self,
        root_dir: Path | str,
        *,
        max_quota_bytes: int | None = None,
    ) -> None:
        if max_quota_bytes is not None and max_quota_bytes <= 0:
            raise PersonalVaultError(
                f"max_quota_bytes must be positive when set, got {max_quota_bytes}"
            )
        raw_root = Path(root_dir)
        if raw_root.is_symlink():
            raise VaultTransportError(f"Vault root directory must not be a symlink: {raw_root}")
        self._root_dir = raw_root.resolve()
        self._chunks_dir = self._root_dir / "chunks"
        self._manifests_dir = self._root_dir / "manifests"
        self._leases_dir = self._root_dir / ".leases"
        self._staging_dir = self._root_dir / ".staging"
        self._max_quota_bytes = max_quota_bytes
        # Leases this instance wrote, as ``stem -> (sha256 of the body, lease token)``.
        # Same rule as the S3 driver: a release without an explicit ETag conditions on
        # our own write, so a refresh by another writer is a conflict, not an unlink.
        self._owned_lease_etags: dict[str, str] = {}
        self._owned_lease_tokens: dict[str, str] = {}

        try:
            self._chunks_dir.mkdir(parents=True, exist_ok=True)
            self._manifests_dir.mkdir(parents=True, exist_ok=True)
            self._leases_dir.mkdir(parents=True, exist_ok=True)
            self._staging_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VaultTransportError(f"Cannot initialize local vault directory: {exc}") from exc

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @property
    def max_quota_bytes(self) -> int | None:
        return self._max_quota_bytes

    def used_bytes(self) -> int:
        """Compute total bytes currently stored across chunks and manifests."""
        total = 0
        for folder in (self._chunks_dir, self._manifests_dir):
            for entry in folder.iterdir():
                if entry.name.startswith("."):
                    continue
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
        return total

    def _enforce_quota(self, target_path: Path, incoming_size: int) -> None:
        if self._max_quota_bytes is None:
            return
        existing_size = target_path.stat().st_size if target_path.is_file() else 0
        projected = self.used_bytes() - existing_size + incoming_size
        if projected > self._max_quota_bytes:
            raise VaultQuotaExceededError(
                f"Local vault quota exceeded: projected {projected} bytes > "
                f"max_quota_bytes {self._max_quota_bytes}"
            )

    def _chunk_path(self, chunk_id: str) -> Path:
        valid = _validate_chunk_id(chunk_id)
        return self._chunks_dir / f"{valid}.chk"

    def _manifest_path(self, snapshot_id: str) -> Path:
        valid = _validate_snapshot_id(snapshot_id)
        return self._manifests_dir / f"{valid}.json"

    def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
        path = self._chunk_path(chunk_id)
        self._enforce_quota(path, len(data))
        _atomic_write_file(path, data)

    def get_chunk(self, chunk_id: str) -> bytes:
        path = self._chunk_path(chunk_id)
        if path.is_symlink() or not path.is_file():
            raise VaultTransportError(f"Chunk not found in local vault: {chunk_id}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise VaultTransportError(f"Failed reading chunk {chunk_id}: {exc}") from exc

    def has_chunk(self, chunk_id: str) -> bool:
        path = self._chunk_path(chunk_id)
        return path.is_file() and not path.is_symlink()

    def get_chunk_mtime(self, chunk_id: str) -> float | None:
        """Return POSIX modification timestamp of ``chunk_id``, or ``None`` if absent."""
        path = self._chunk_path(chunk_id)
        if path.is_symlink() or not path.is_file():
            return None
        return path.stat().st_mtime

    def put_manifest(self, snapshot_id: str, data: bytes) -> None:
        path = self._manifest_path(snapshot_id)
        self._enforce_quota(path, len(data))
        _atomic_write_file(path, data)

    def get_manifest(self, snapshot_id: str) -> bytes:
        path = self._manifest_path(snapshot_id)
        if path.is_symlink() or not path.is_file():
            raise VaultTransportError(f"Manifest not found in local vault: {snapshot_id}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise VaultTransportError(f"Failed reading manifest {snapshot_id}: {exc}") from exc

    def list_manifests(self) -> list[str]:
        manifests: list[str] = []
        for entry in self._manifests_dir.iterdir():
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".json":
                if not entry.name.startswith("."):
                    manifests.append(entry.stem)
        return sorted(manifests)

    def list_chunks(self) -> list[str]:
        """Return sorted chunk IDs currently stored in the local vault."""
        chunks: list[str] = []
        for entry in self._chunks_dir.iterdir():
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".chk":
                if not entry.name.startswith("."):
                    chunks.append(entry.stem)
        return sorted(chunks)

    def delete_chunk(self, chunk_id: str) -> None:
        path = self._chunk_path(chunk_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise VaultTransportError(f"Failed deleting chunk {chunk_id}: {exc}") from exc

    def delete_manifest(self, snapshot_id: str) -> None:
        """Delete a snapshot manifest file from the local vault."""
        path = self._manifest_path(snapshot_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise VaultTransportError(f"Failed deleting manifest {snapshot_id}: {exc}") from exc

    def _lease_path(self, lease_id: str) -> Path:
        valid = _validate_snapshot_id(lease_id)
        return self._leases_dir / f"lease-{valid}.json"

    @contextlib.contextmanager
    def _coordination_lock(
        self,
        *,
        timeout_seconds: float = 10.0,
        stale_lock_seconds: float = 30.0,
    ) -> Iterator[None]:
        """Cross-process mutex closing the GC/lease TOCTOU window without lock theft.

        Delegates to :func:`_cross_process_file_lock`, which layers a POSIX kernel
        ``flock`` over an ownership-verified ``O_CREAT | O_EXCL`` fallback so a lock is
        never taken over from a live owner and never unlinked by a stale owner.
        """
        self._leases_dir.mkdir(parents=True, exist_ok=True)
        with _cross_process_file_lock(
            self._leases_dir / _GC_COORD_LOCK_FILENAME,
            owner_path=self._leases_dir / _GC_COORD_OWNER_FILENAME,
            timeout_seconds=timeout_seconds,
            stale_lock_seconds=stale_lock_seconds,
        ):
            yield

    def acquire_push_lease(
        self,
        chunk_ids: Iterable[str] = (),
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
        lease_id: str | None = None,
    ) -> str:
        """Atomically create a cross-process lease file via ``O_CREAT | O_EXCL | O_WRONLY``."""
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        now = time.time()
        effective_id = (
            f"{os.getpid()}-{int(now * 1000)}-{secrets.token_hex(6)}"
            if lease_id is None
            else _validate_snapshot_id(lease_id)
        )
        lease_path = self._lease_path(effective_id)
        local_lease_token = uuid.uuid4().hex
        payload = _build_lease_json_bytes(
            lease_id=effective_id,
            project_id=project_id,
            now_epoch=now,
            ttl_seconds=finite_ttl,
            chunk_ids=chunk_ids,
            lease_token=local_lease_token,
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        with self._coordination_lock():
            try:
                fd = os.open(lease_path, flags, 0o600)
            except FileExistsError as exc:
                raise VaultRevisionConflictError(
                    f"Push lease {effective_id!r} already exists in {self._leases_dir}"
                ) from exc
            except OSError as exc:
                raise VaultTransportError(
                    f"Failed creating push lease {lease_path}: {exc}"
                ) from exc
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        self._owned_lease_etags[effective_id] = hashlib.sha256(payload).hexdigest()
        self._owned_lease_tokens[effective_id] = local_lease_token
        return effective_id

    def update_push_lease(
        self,
        lease_id: str,
        chunk_ids: Iterable[str],
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
        expected_etag: str | None = None,
    ) -> bool:
        """Refresh only a current lease owned by this instance's token and ETag pair."""
        valid_id = _validate_snapshot_id(lease_id)
        owned = _owned_lease_pair(
            self._owned_lease_tokens, self._owned_lease_etags, valid_id, expected_etag
        )
        if owned is None:
            with self._coordination_lock():
                current = _read_lease_file_once(self._lease_path(valid_id))
            if current is None:
                raise VaultTransportError(f"Push lease {valid_id!r} is absent")
            raise VaultRevisionConflictError(
                f"This driver cannot prove ownership of lease {valid_id!r}"
            )
        owned_token, owned_etag = owned
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        lease_path = self._lease_path(valid_id)
        now = time.time()
        refreshed_token = uuid.uuid4().hex
        with self._coordination_lock():
            current = _read_lease_file_once(lease_path)
            if current is None:
                self._owned_lease_tokens.pop(valid_id, None)
                self._owned_lease_etags.pop(valid_id, None)
                raise VaultRevisionConflictError(
                    f"Lease {valid_id!r} changed owner or ETag before refresh"
                )
            current_etag = hashlib.sha256(current).hexdigest()
            lease_doc_id, actual_project, _verifier = _lease_owner_fields(current)
            if (
                current_etag != owned_etag
                or (project_id and actual_project != project_id)
                or not _lease_record_matches_owner(
                    current,
                    project_id=actual_project,
                    lease_id=valid_id,
                    lease_token=owned_token,
                )
            ):
                self._owned_lease_tokens.pop(valid_id, None)
                self._owned_lease_etags.pop(valid_id, None)
                raise VaultRevisionConflictError(
                    f"Lease {valid_id!r} changed owner or ETag before refresh"
                )
            if lease_doc_id != valid_id:
                return False
            expires_at, _chunks = _parse_and_validate_lease_payload(
                current, source_label=str(lease_path)
            )
            if expires_at <= now:
                return False
            payload = _build_lease_json_bytes(
                lease_id=valid_id,
                project_id=actual_project,
                now_epoch=now,
                ttl_seconds=finite_ttl,
                chunk_ids=chunk_ids,
                lease_token=refreshed_token,
            )
            _atomic_write_file(lease_path, payload)
        self._owned_lease_etags[valid_id] = hashlib.sha256(payload).hexdigest()
        self._owned_lease_tokens[valid_id] = refreshed_token
        return True

    def get_owned_lease_token(self, lease_id: str) -> str:
        """Return a private credential only after re-proving its current owner pair."""
        return self.export_lease_credential(lease_id)["lease_token"]

    def export_lease_credential(self, lease_id: str) -> dict[str, str]:
        """Export the current credential for private owner storage, never from shared data."""
        valid_id = _validate_snapshot_id(lease_id)
        owned = _owned_lease_pair(self._owned_lease_tokens, self._owned_lease_etags, valid_id, None)
        if owned is None:
            raise VaultDriverError(f"This driver does not own push lease {valid_id!r}")
        token, owned_etag = owned
        with self._coordination_lock():
            raw = _read_lease_file_once(self._lease_path(valid_id))
            if raw is None:
                raise VaultDriverError(f"Push lease {valid_id!r} is absent")
            current_etag = hashlib.sha256(raw).hexdigest()
            actual_id, project_id, _verifier = _lease_owner_fields(raw)
            if (
                current_etag != owned_etag
                or actual_id != valid_id
                or not _lease_record_matches_owner(
                    raw, project_id=project_id, lease_id=valid_id, lease_token=token
                )
            ):
                raise VaultDriverError(f"Push lease {valid_id!r} is no longer owned here")
        return {
            "lease_id": valid_id,
            "project_id": project_id,
            "lease_token": token,
            "etag": current_etag,
        }

    def recover_owned_lease(
        self,
        project_id: str,
        lease_id: str,
        lease_token: str,
        expected_etag: str | None = None,
    ) -> str:
        """Prove the secret token against an active lease verifier before restoring ownership."""
        valid_id = _validate_snapshot_id(lease_id)
        if not isinstance(lease_token, str) or not lease_token:
            raise VaultDriverError("Lease recovery requires a non-empty owner token")
        with self._coordination_lock():
            path = self._lease_path(valid_id)
            raw = _read_lease_file_once(path)
            if raw is None:
                raise VaultDriverError(f"Push lease {valid_id!r} is absent")
            expires_at, _chunks = _parse_and_validate_lease_payload(raw, source_label=str(path))
            if expires_at <= time.time():
                raise VaultDriverError(f"Push lease {valid_id!r} has expired")
            verifier = _lease_verifier_for_recovery(
                raw, project_id=project_id, lease_id=valid_id, source_label=str(path)
            )
            expected_verifier = _compute_lease_token_verifier(project_id, valid_id, lease_token)
            if not hmac.compare_digest(expected_verifier, verifier):
                raise VaultDriverError(f"Push lease {valid_id!r} owner token did not verify")
            current_etag = hashlib.sha256(raw).hexdigest()
            if (
                not isinstance(expected_etag, str)
                or not expected_etag
                or not hmac.compare_digest(current_etag, expected_etag)
            ):
                raise VaultDriverError(f"Push lease {valid_id!r} ETag did not verify")
            self._owned_lease_tokens[valid_id] = lease_token
            self._owned_lease_etags[valid_id] = current_etag
            return current_etag

    def release_push_lease(self, lease_id: str, *, expected_etag: str | None = None) -> bool:
        """Release only a lease proven by this instance's private token and exact ETag."""
        valid_id = _validate_snapshot_id(lease_id)
        owned = _owned_lease_pair(
            self._owned_lease_tokens, self._owned_lease_etags, valid_id, expected_etag
        )
        if owned is None:
            with self._coordination_lock():
                current = _read_lease_file_once(self._lease_path(valid_id))
            if current is None:
                return False
            raise VaultRevisionConflictError(
                f"This driver cannot prove ownership of lease {valid_id!r}"
            )
        owned_token, owned_etag = owned
        lease_path = self._lease_path(valid_id)
        with self._coordination_lock():
            current = _read_lease_file_once(lease_path)
            if current is None:
                self._owned_lease_tokens.pop(valid_id, None)
                self._owned_lease_etags.pop(valid_id, None)
                return False
            current_etag = hashlib.sha256(current).hexdigest()
            actual_id, actual_project, _verifier = _lease_owner_fields(current)
            if (
                current_etag != owned_etag
                or actual_id != valid_id
                or not _lease_record_matches_owner(
                    current,
                    project_id=actual_project,
                    lease_id=valid_id,
                    lease_token=owned_token,
                )
            ):
                self._owned_lease_tokens.pop(valid_id, None)
                self._owned_lease_etags.pop(valid_id, None)
                return False
            try:
                lease_path.unlink()
            except OSError as exc:
                raise VaultTransportError(
                    f"Failed releasing push lease {valid_id!r}: {exc}"
                ) from exc
            self._owned_lease_tokens.pop(valid_id, None)
            self._owned_lease_etags.pop(valid_id, None)
        return True

    def _read_active_leased_chunks_unlocked(self, *, effective_now: float) -> set[str]:
        """Parse every lease file, failing closed on ANY unexpected entry.

        Only the two coordination artefacts owned by :meth:`_coordination_lock`
        (``.gc_coord.lock`` and the ``.gc_coord.owner`` fallback) are exempt. Every
        other entry — hidden dot-file, symlink, sub-directory, or misnamed regular
        file — makes the lease state ambiguous and therefore raises
        :class:`VaultTransportError` so GC deletes nothing.
        """
        active_chunks: set[str] = set()
        for entry in sorted(self._leases_dir.iterdir()):
            if entry.name in (_GC_COORD_LOCK_FILENAME, _GC_COORD_OWNER_FILENAME):
                continue
            if (
                entry.is_symlink()
                or not entry.is_file()
                or not _LEASE_DIR_FILENAME_RE.fullmatch(entry.name)
            ):
                raise VaultTransportError(
                    f"Unexpected, hidden or symlinked entry in vault lease directory: {entry}"
                )
            try:
                raw_bytes = _read_lease_file_once(entry)
            except OSError as exc:
                raise VaultTransportError(f"Failed reading push lease file {entry}: {exc}") from exc
            if raw_bytes is None:
                raise VaultRevisionConflictError(
                    f"Push lease {entry} disappeared while the lease directory was scanned"
                )
            expires_at, chunk_set = _parse_and_validate_lease_payload(
                raw_bytes,
                source_label=str(entry),
            )
            if expires_at <= effective_now:
                with contextlib.suppress(OSError):
                    entry.unlink(missing_ok=True)
                continue
            active_chunks.update(chunk_set)
        return active_chunks

    def read_active_leased_chunks(self, *, now_epoch: float | None = None) -> set[str]:
        """Read unexpired in-flight chunk IDs fail-closed across cross-process lease files."""
        effective_now = _validate_finite_epoch(now_epoch, default_now=time.time())
        with self._coordination_lock():
            return self._read_active_leased_chunks_unlocked(effective_now=effective_now)

    def delete_chunk_if_unleased(
        self,
        chunk_id: str,
        *,
        now_epoch: float | None = None,
    ) -> bool:
        """Atomically re-verify leases under ``.gc_coord.lock`` and delete ``chunk_id``."""
        valid = _validate_chunk_id(chunk_id)
        effective_now = _validate_finite_epoch(now_epoch, default_now=time.time())
        with self._coordination_lock():
            active_leased = self._read_active_leased_chunks_unlocked(effective_now=effective_now)
            if valid in active_leased or not self.has_chunk(valid):
                return False
            self.delete_chunk(valid)
            return True

    def _staging_journal_path(self, project_id: str, parent_snapshot_id: str | None) -> Path:
        digest = _staging_journal_digest(project_id, parent_snapshot_id)
        return self._staging_dir / f"stage-{digest}.json"

    def save_staging_journal(self, journal: VaultStagingJournal) -> None:
        """Atomically persist a crash-recovery staging journal."""
        path = self._staging_journal_path(journal.project_id, journal.parent_snapshot_id)
        _atomic_write_file(path, journal.to_json_bytes())

    def load_staging_journal(
        self,
        project_id: str,
        parent_snapshot_id: str | None,
    ) -> VaultStagingJournal | None:
        """Load an active crash-recovery staging journal if present and valid."""
        path = self._staging_journal_path(project_id, parent_snapshot_id)
        if path.is_symlink() or not path.is_file():
            return None
        try:
            journal = VaultStagingJournal.from_json_bytes(path.read_bytes())
        except (OSError, ValueError, KeyError, TypeError, PersonalVaultError):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        if journal.project_id != project_id or journal.parent_snapshot_id != parent_snapshot_id:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        return journal

    def clear_staging_journal(self, project_id: str, parent_snapshot_id: str | None) -> None:
        """Remove a completed staging journal."""
        path = self._staging_journal_path(project_id, parent_snapshot_id)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 2. S3CompatibleSigV4Driver (AWS4-HMAC-SHA256 — R2 / AWS S3 / MinIO)
# --------------------------------------------------------------------------- #


def _sigv4_uri_encode(value: str, *, encode_slash: bool) -> str:
    safe_chars = "-_.~" if encode_slash else "/-_.~"
    return urllib.parse.quote(value, safe=safe_chars)


def _hmac_sha256_bytes(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


@dataclass(frozen=True, slots=True)
class SigV4Artifacts:
    """Structured AWS Signature Version 4 signing outputs for auditing and tests."""

    canonical_request: str
    string_to_sign: str
    signature: str
    authorization_header: str
    signed_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class S3SigV4Config:
    """Connection and credential settings for an S3-compatible user bucket."""

    endpoint_url: str
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    prefix: str = "nagar-vault"
    service: str = "s3"
    path_style: bool = True
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.endpoint_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PersonalVaultError(
                f"endpoint_url must be a valid http(s) URL, got {self.endpoint_url!r}"
            )
        if not self.bucket or "/" in self.bucket:
            raise PersonalVaultError(f"Invalid S3 bucket name: {self.bucket!r}")
        if not self.region.strip():
            raise PersonalVaultError("S3 region must not be empty")
        if not self.access_key_id.strip() or not self.secret_access_key:
            raise VaultAuthenticationError("S3 access_key_id and secret_access_key are required")


@dataclass(frozen=True, slots=True)
class S3HttpRequest:
    """Immutable HTTP request passed to ``http_transport``."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class S3HttpResponse:
    """Immutable HTTP response returned by ``http_transport``."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


HttpTransportCallable = Callable[[S3HttpRequest], S3HttpResponse]


def compute_sigv4_signature(
    *,
    method: str,
    url: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    service: str = "s3",
    payload: bytes | bytearray | memoryview = b"",
    extra_headers: Mapping[str, str] | None = None,
    session_token: str | None = None,
    timestamp: datetime | None = None,
) -> SigV4Artifacts:
    """Compute standard ``AWS4-HMAC-SHA256`` request signature using Python stdlib."""
    if not access_key_id or not secret_access_key:
        raise VaultAuthenticationError("AWS SigV4 credentials must not be empty")

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PersonalVaultError(f"SigV4 target must be an http(s) URL, got {url!r}")

    now_utc = (
        datetime.now(timezone.utc) if timestamp is None else timestamp.astimezone(timezone.utc)
    )
    amz_date = now_utc.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now_utc.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    raw_path = parsed.path if parsed.path else "/"
    unquoted_path = urllib.parse.unquote(raw_path)
    canonical_uri = _sigv4_uri_encode(unquoted_path, encode_slash=False)

    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    encoded_pairs = sorted(
        (
            _sigv4_uri_encode(key, encode_slash=True),
            _sigv4_uri_encode(val, encode_slash=True),
        )
        for key, val in query_pairs
    )
    canonical_query = "&".join(f"{key}={val}" for key, val in encoded_pairs)

    header_map: dict[str, str] = {
        "host": parsed.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        header_map["x-amz-security-token"] = session_token
    if extra_headers:
        for key, value in extra_headers.items():
            normalized_val = " ".join(value.strip().split())
            header_map[key.strip().lower()] = normalized_val

    sorted_header_items = sorted(header_map.items(), key=lambda item: item[0])
    canonical_headers = "".join(f"{key}:{val}\n" for key, val in sorted_header_items)
    signed_header_names = ";".join(key for key, _ in sorted_header_items)

    http_method = method.upper()
    canonical_request = "\n".join(
        [
            http_method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_header_names,
            payload_hash,
        ]
    )
    canonical_request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            canonical_request_hash,
        ]
    )

    k_date = _hmac_sha256_bytes(f"AWS4{secret_access_key}".encode(), date_stamp)
    k_region = _hmac_sha256_bytes(k_date, region)
    k_service = _hmac_sha256_bytes(k_region, service)
    k_signing = _hmac_sha256_bytes(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )

    final_headers = dict(sorted_header_items)
    final_headers["Authorization"] = authorization_header
    return SigV4Artifacts(
        canonical_request=canonical_request,
        string_to_sign=string_to_sign,
        signature=signature,
        authorization_header=authorization_header,
        signed_headers=final_headers,
    )


def _default_urllib_transport(request: S3HttpRequest, *, timeout: float) -> S3HttpResponse:
    """Execute ``request`` using Python's stdlib :mod:`urllib.request`."""
    import urllib.error
    import urllib.request

    parsed = urllib.parse.urlsplit(request.url)
    if parsed.scheme not in ("http", "https"):
        raise VaultTransportError(f"Disallowed URL scheme: {parsed.scheme!r}")
    data = request.body if request.method in ("PUT", "POST") else None
    req = urllib.request.Request(
        url=request.url,
        data=data,
        headers=request.headers,
        method=request.method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return S3HttpResponse(
                status_code=int(resp.status),
                headers=resp_headers,
                body=resp.read(),
            )
    except urllib.error.HTTPError as exc:
        err_body = exc.read() if exc.fp is not None else b""
        err_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return S3HttpResponse(
            status_code=int(exc.code),
            headers=err_headers,
            body=err_body,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VaultTransportError(f"S3 HTTP transport error: {exc}") from exc


_XML_ENTITY_REF_RE = re.compile(rb"&([a-zA-Z_][a-zA-Z0-9._-]*);")
_ALLOWED_XML_ENTITIES = frozenset({b"amp", b"lt", b"gt", b"quot", b"apos"})
_FORBIDDEN_XML_TOKENS = (
    b"<!doctype",
    b"<!entity",
    b"<!element",
    b"<!attlist",
    b"<!notation",
    b"<![cdata[",
)


def _parse_s3_list_xml_entries(
    xml_bytes: bytes,
) -> tuple[list[tuple[str, float | None]], str | None]:
    """Safely parse S3 ``ListObjectsV2`` XML keys and optional ``<LastModified>`` timestamps."""
    if b"\x00" in xml_bytes:
        raise VaultTransportError("NUL byte forbidden in S3 ListObjectsV2 XML response")
    lowered = xml_bytes.lower()
    for token in _FORBIDDEN_XML_TOKENS:
        if token in lowered:
            raise VaultTransportError(
                f"Forbidden XML construct {token.decode('ascii')!r} in S3 ListObjectsV2 response"
            )
    for match in _XML_ENTITY_REF_RE.finditer(lowered):
        if match.group(1) not in _ALLOWED_XML_ENTITIES:
            raise VaultTransportError(
                "Forbidden custom XML entity reference in S3 ListObjectsV2 response"
            )
    try:
        root = ET.fromstring(xml_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise VaultTransportError("Malformed XML in S3 ListObjectsV2 response") from exc

    entries: list[tuple[str, float | None]] = []
    is_truncated = False
    next_token: str | None = None

    for child in root:
        local_child = child.tag.rsplit("}", 1)[-1]
        if local_child == "Contents":
            item_key: str | None = None
            item_mtime: float | None = None
            for sub in child:
                sub_name = sub.tag.rsplit("}", 1)[-1]
                if sub_name == "Key" and sub.text:
                    item_key = sub.text
                elif sub_name == "LastModified" and sub.text:
                    item_mtime = _parse_http_or_iso_timestamp(sub.text)
            if item_key is not None:
                entries.append((item_key, item_mtime))
        elif local_child == "IsTruncated" and child.text:
            is_truncated = child.text.strip().lower() == "true"
        elif local_child == "NextContinuationToken" and child.text:
            next_token = child.text.strip()

    return entries, (next_token if is_truncated else None)


def _parse_s3_list_xml(xml_bytes: bytes) -> tuple[list[str], str | None]:
    """Safely parse S3 ``ListObjectsV2`` XML response without DTD/entity/CDATA constructs."""
    entries, next_token = _parse_s3_list_xml_entries(xml_bytes)
    return [key for key, _ in entries], next_token


class S3CompatibleSigV4Driver:
    """Direct user-bucket BYOS driver (Cloudflare R2 / AWS S3 / MinIO) via SigV4."""

    __slots__ = (
        "_config",
        "_transport",
        "_clock",
        "_clean_prefix",
        "_chunk_mtimes",
        "_owned_lease_etags",
        "_owned_lease_tokens",
        "_coord_lock",
        "_fence_race_probe",
        "_post_final_check_race_probe",
        "_pre_lease_put_race_probe",
        "_clock_skew_margin",
        "__dict__",  # preserve the instance-level test seams and subclass compatibility
    )

    def __init__(
        self,
        config: S3SigV4Config,
        *,
        http_transport: HttpTransportCallable | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._transport: HttpTransportCallable = (
            (lambda req: _default_urllib_transport(req, timeout=config.timeout_seconds))
            if http_transport is None
            else http_transport
        )
        self._clock: Callable[[], datetime] = (
            (lambda: datetime.now(timezone.utc)) if clock is None else clock
        )
        self._clean_prefix = config.prefix.strip("/")
        self._chunk_mtimes: dict[str, float] = {}
        # Private instance ownership credentials. Shared lease bytes contain only a
        # verifier; release still requires this exact token/ETag pair.
        self._owned_lease_etags: dict[str, str] = {}
        self._owned_lease_tokens: dict[str, str] = {}
        # Serialises lease acquisition against GC within this process; cross-process
        # safety still comes from the conditional lease writes and the fence protocol.
        self._coord_lock = threading.RLock()
        # Test seams, ``None`` in production. Both mark a window a test cannot
        # otherwise reach deterministically, because each is bounded by one network
        # round trip inside ``delete_chunk_if_unleased``:
        #   * ``_fence_race_probe``      — before the final lease-fence decision;
        #   * ``_post_final_check_race_probe`` — *after* that decision succeeded and
        #     immediately before the fence object is retired. A push landing here is
        #     the audited race. See tests/unit/test_personal_cloud_vault_lease_fence.py.
        self._fence_race_probe: Callable[[], None] | None = None
        self._post_final_check_race_probe: Callable[[], None] | None = None
        #   * ``_pre_lease_put_race_probe`` — on the *writer* side, after the preflight
        #     fence/existence checks and immediately before the lease object is
        #     written. A collector that runs to completion here is the ABA race: the
        #     fence it leaves behind is gone by the time the writer looks again.
        self._pre_lease_put_race_probe: Callable[[], None] | None = None
        # Leeway applied whenever this driver has to decide that *another* host's
        # claim is dead. Conservative by construction: it can only extend a claim's
        # life, never shorten it.
        self._clock_skew_margin: float = CLOCK_SKEW_SAFETY_MARGIN_SECONDS

    @property
    def config(self) -> S3SigV4Config:
        return self._config

    def _object_key(self, category: str, leaf_name: str) -> str:
        if self._clean_prefix:
            return f"{self._clean_prefix}/{category}/{leaf_name}"
        return f"{category}/{leaf_name}"

    def _gc_fence_key(self, chunk_id: str) -> str:
        """Object key of the GC fence guarding ``chunk_id`` against a concurrent lease."""
        valid = _validate_chunk_id(chunk_id)
        return self._object_key("gc-fences", f"{valid}.fence")

    def _build_url(
        self, object_key: str | None = None, query: Mapping[str, str] | None = None
    ) -> str:
        parsed = urllib.parse.urlsplit(self._config.endpoint_url)
        base_path = parsed.path.rstrip("/")
        if self._config.path_style:
            host = parsed.netloc
            if object_key:
                full_path = f"{base_path}/{self._config.bucket}/{object_key}"
            else:
                full_path = f"{base_path}/{self._config.bucket}"
        else:
            host = f"{self._config.bucket}.{parsed.netloc}"
            full_path = f"{base_path}/{object_key}" if object_key else (base_path or "/")

        query_str = urllib.parse.urlencode(sorted(query.items())) if query else ""
        return urllib.parse.urlunsplit((parsed.scheme, host, full_path, query_str, ""))

    def _send(
        self,
        method: str,
        *,
        object_key: str | None = None,
        query: Mapping[str, str] | None = None,
        body: bytes = b"",
        content_type: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> S3HttpResponse:
        url = self._build_url(object_key=object_key, query=query)
        extra: dict[str, str] = dict(extra_headers) if extra_headers else {}
        if content_type is not None:
            extra["content-type"] = content_type
        if method in ("PUT", "POST"):
            extra["content-length"] = str(len(body))

        artifacts = compute_sigv4_signature(
            method=method,
            url=url,
            access_key_id=self._config.access_key_id,
            secret_access_key=self._config.secret_access_key,
            region=self._config.region,
            service=self._config.service,
            payload=body,
            extra_headers=extra,
            session_token=self._config.session_token,
            timestamp=self._clock(),
        )
        request = S3HttpRequest(
            method=method.upper(),
            url=url,
            headers=artifacts.signed_headers,
            body=body,
        )
        response = self._transport(request)
        if response.status_code in (401, 403):
            raise VaultAuthenticationError(
                f"S3 authentication/authorization rejected ({response.status_code}) for {method}"
            )
        if response.status_code == 507:
            raise VaultQuotaExceededError("S3 bucket quota exceeded (HTTP 507)")
        return response

    def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
        valid = _validate_chunk_id(chunk_id)
        # The S3 transport (SigV4 signing + HTTP body) is a single-chunk-bounded copy;
        # larger-than-chunk materialisation never happens here.
        body = data if isinstance(data, bytes) else bytes(data)
        resp = self._send(
            "PUT",
            object_key=self._object_key("chunks", f"{valid}.chk"),
            body=body,
            content_type="application/octet-stream",
            # Content-addressed ids make the ETag a function of the bytes alone, so
            # every upload carries its own generation: a re-upload of identical bytes
            # is a *new* object, and the lease proof must be able to see that.
            extra_headers={CHUNK_GENERATION_META_HEADER: _new_chunk_generation()},
        )
        if resp.status_code not in (200, 201, 204):
            raise VaultTransportError(f"S3 put_chunk failed with HTTP {resp.status_code}")
        self._chunk_mtimes[valid] = self._clock().timestamp()

    def get_chunk(self, chunk_id: str) -> bytes:
        valid = _validate_chunk_id(chunk_id)
        resp = self._send("GET", object_key=self._object_key("chunks", f"{valid}.chk"))
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 get_chunk failed for {valid} with HTTP {resp.status_code}"
            )
        return resp.body

    def has_chunk(self, chunk_id: str) -> bool:
        valid = _validate_chunk_id(chunk_id)
        resp = self._send("HEAD", object_key=self._object_key("chunks", f"{valid}.chk"))
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        raise VaultTransportError(f"S3 has_chunk failed for {valid} with HTTP {resp.status_code}")

    def get_chunk_mtime(self, chunk_id: str) -> float | None:
        """Return UTC epoch mtime of ``chunk_id`` from S3 ``Last-Modified`` or metadata."""
        valid = _validate_chunk_id(chunk_id)
        resp = self._send("HEAD", object_key=self._object_key("chunks", f"{valid}.chk"))
        if resp.status_code == 404:
            self._chunk_mtimes.pop(valid, None)
            return None
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 get_chunk_mtime failed for {valid} with HTTP {resp.status_code}"
            )
        lowered_headers = {k.lower(): v for k, v in resp.headers.items()}
        raw_lm = lowered_headers.get("last-modified")
        if raw_lm is not None:
            parsed_ts = _parse_http_or_iso_timestamp(raw_lm)
            if parsed_ts is not None:
                self._chunk_mtimes[valid] = parsed_ts
                return parsed_ts
        return self._chunk_mtimes.get(valid)

    def put_manifest(self, snapshot_id: str, data: bytes) -> None:
        valid = _validate_snapshot_id(snapshot_id)
        resp = self._send(
            "PUT",
            object_key=self._object_key("manifests", f"{valid}.json"),
            body=data,
            content_type="application/json",
        )
        if resp.status_code not in (200, 201, 204):
            raise VaultTransportError(f"S3 put_manifest failed with HTTP {resp.status_code}")

    def get_manifest(self, snapshot_id: str) -> bytes:
        valid = _validate_snapshot_id(snapshot_id)
        resp = self._send("GET", object_key=self._object_key("manifests", f"{valid}.json"))
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 get_manifest failed for {valid} with HTTP {resp.status_code}"
            )
        return resp.body

    def _list_category_stems(self, category: str, suffix: str) -> list[str]:
        prefix = self._object_key(category, "")
        stems: list[str] = []
        continuation: str | None = None
        while True:
            query: dict[str, str] = {"list-type": "2", "prefix": prefix}
            if continuation is not None:
                query["continuation-token"] = continuation
            resp = self._send("GET", query=query)
            if resp.status_code != 200:
                raise VaultTransportError(
                    f"S3 list ({category}) failed with HTTP {resp.status_code}"
                )
            entries, continuation = _parse_s3_list_xml_entries(resp.body)
            for key, mtime_val in entries:
                if key.startswith(prefix) and key.endswith(suffix):
                    stem = key[len(prefix) : -len(suffix)]
                    if stem and "/" not in stem:
                        stems.append(stem)
                        if category == "chunks" and mtime_val is not None:
                            self._chunk_mtimes[stem] = mtime_val
            if continuation is None:
                break
        return sorted(set(stems))

    def list_manifests(self) -> list[str]:
        return self._list_category_stems("manifests", ".json")

    def list_chunks(self) -> list[str]:
        """List all chunk IDs stored under this bucket prefix."""
        return self._list_category_stems("chunks", ".chk")

    def delete_chunk(self, chunk_id: str) -> None:
        valid = _validate_chunk_id(chunk_id)
        resp = self._send("DELETE", object_key=self._object_key("chunks", f"{valid}.chk"))
        if resp.status_code not in (200, 202, 204, 404):
            raise VaultTransportError(
                f"S3 delete_chunk failed for {valid} with HTTP {resp.status_code}"
            )
        self._chunk_mtimes.pop(valid, None)

    def _assert_no_live_gc_fence(
        self,
        chunk_ids: Iterable[str],
        *,
        now_epoch: float,
        when: str,
    ) -> None:
        """Refuse to publish a lease over a chunk a collector is deleting right now.

        ``when`` names the half of the handshake this is (before the lease write, or
        the read-after-write half), so the raised message says which side observed
        the collision. A *dead* fence is deliberately not a refusal: it is debris
        from a collector that crashed, and it is reclaimed separately
        (:meth:`_reclaim_dead_gc_fences`).
        """
        fenced = self._live_gc_fenced_chunks(chunk_ids, now_epoch=now_epoch)
        if fenced:
            raise VaultRevisionConflictError(
                "S3 GC fence is live for chunk(s) "
                f"{sorted(fenced)} ({when}); refusing to lease a chunk a collector "
                "is deleting. Retry once the collection finishes."
            )

    def _reclaim_dead_gc_fences(self, chunk_ids: Iterable[str], *, now_epoch: float) -> None:
        """Clear provably expired fences — but only for chunks proven to still exist.

        A dead fence is removed because the caller now holds a lease that protects
        the chunk; if the chunk is *missing*, that collector may have completed its
        delete before dying, and erasing its fence would erase the only evidence.
        """
        for cid in chunk_ids:
            fence = self._read_gc_fence(cid)
            if fence is None or fence.is_live(now_epoch, margin_seconds=self._clock_skew_margin):
                continue
            if not self.has_chunk(cid):
                continue
            self._delete_gc_fence_if_unchanged(cid, fence)

    def _chunk_generation(self, chunk_id: str) -> str | None:
        """Identity of the object currently stored under ``chunk_id``.

        Identity is the tuple ``(version-id, generation stamp, ETag)`` rendered as one
        string, because any single component is forgeable by a rewrite:

        * the **ETag** is a hash of the bytes, so deleting a chunk and re-uploading the
          same bytes reproduces it exactly (the ABA replacement a collector can leave);
        * the **generation stamp** is the metadata header every upload mints, so a
          rewrite is visible even when the bytes are identical;
        * the **version-id** is the bucket's own object version, and advances on *every*
          write when versioning is on.

        Return values keep three distinct states: ``None`` = "not on the bucket",
        ``UNKNOWN_GENERATION`` = "present but the bucket identifies neither the version
        nor the upload", anything else = a comparable identity. The middle state is
        fail-closed: an object whose identity cannot be proven cannot be silently
        treated as unchanged.
        """
        resp = self._send("HEAD", object_key=self._object_key("chunks", f"{chunk_id}.chk"))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 chunk generation probe failed for {chunk_id} with HTTP {resp.status_code}"
            )
        lowered = {k.lower(): v for k, v in resp.headers.items()}
        version_id = lowered.get("x-amz-version-id", "").strip()
        generation = lowered.get(CHUNK_GENERATION_META_HEADER, "").strip()
        etag = lowered.get("etag", "").strip()
        if not version_id and not generation:
            return UNKNOWN_CHUNK_GENERATION
        return f"{version_id}|{generation}|{etag}"

    def _chunk_generations(self, chunk_ids: Iterable[str]) -> dict[str, str | None]:
        """Snapshot every chunk's generation *before* the lease object is written."""
        return {cid: self._chunk_generation(cid) for cid in chunk_ids}

    def _lost_materialised_chunks(self, preflight: Mapping[str, str | None]) -> tuple[str, ...]:
        """Chunks that existed at preflight and no longer do after the lease write.

        A chunk that was *absent* at preflight is skipped on purpose: the push path
        takes its lease first and uploads the chunk immediately afterwards, so
        "absent" there means "the caller is about to write it". A collector cannot
        delete what did not exist, so there is nothing to prove and nothing to break.

        This is the topological half of the ABA fix. By the time this returns, the
        writer's lease object is already on the bucket, so both orders are safe: a
        collector that finished before the lease was written left no chunk behind
        (detected here), and a collector that starts after it sees the lease (and
        refuses in ``delete_chunk_if_unleased``).
        """
        lost: list[str] = []
        for cid, before in preflight.items():
            if before is None:
                continue
            current = self._chunk_generation(cid)
            if current is None:
                lost.append(cid)
            elif UNKNOWN_CHUNK_GENERATION in (before, current):
                # The bucket cannot say which object this is, before or after. Claiming
                # "unchanged" would be a guess about the one thing the proof exists for.
                lost.append(cid)
            elif current != before:
                lost.append(cid)
        return tuple(sorted(lost))

    @staticmethod
    def _lost_chunk_error(lost: tuple[str, ...], when: str) -> VaultRevisionConflictError:
        return VaultRevisionConflictError(
            f"S3 chunk(s) {list(lost)} disappeared or changed generation {when}: a "
            "collector completed a full pass between this lease's preflight and its "
            "write. The lease was rolled back instead of promising a chunk that is no "
            "longer on the bucket. Retry the push."
        )

    def acquire_push_lease(
        self,
        chunk_ids: Iterable[str] = (),
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
        lease_id: str | None = None,
    ) -> str:
        """Create a lease object under ``leases/<lease_id>.json`` on the S3 bucket.

        A lease is a promise that the chunks it names are protected. Across
        processes that promise is only worth something if the collector and the
        pusher agree on a handshake, so this method implements the pusher's half of
        it (the collector's half is :meth:`delete_chunk_if_unleased`):

        * **before** the lease is written, every chunk is checked for a *live* GC
          fence. A live fence means a collector is mid-pass on that chunk, so the
          acquisition is refused (:class:`VaultRevisionConflictError`) rather than
          raced; the same pass records each chunk's *generation* (its ETag);
        * the lease is written with ``If-None-Match: *`` on a fresh id;
        * **after** the lease is written, fences are read again (write-then-read, the
          Dekker half that makes "both sides miss" impossible), every chunk that
          existed at preflight must still exist with the same generation, and fences
          are checked once more after that proof.

        The generation re-check is what closes the *ABA* window. A collector that runs
        entirely inside the gap between the preflight and the write leaves no fence
        behind — it created one, deleted the chunk, retired the fence and returned
        ``True`` — so "is there a live fence now?" answers "no" on the second look, and
        a fence-only protocol would happily publish a lease over a chunk that no
        longer exists. Existence-after-write has no such blind spot: the lease is
        already on the bucket when the chunk is looked for, so exactly one of the two
        orders holds, and both are safe.

        Any refusal rolls the just-written lease back before propagating: a lease that
        outlived the refusal would protect a chunk the collector is already deleting.
        """
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        materialised_chunks = [_validate_chunk_id(cid) for cid in chunk_ids]
        now = self._clock().timestamp()
        effective_id = (
            f"{os.getpid()}-{int(now * 1000)}-{secrets.token_hex(6)}"
            if lease_id is None
            else _validate_snapshot_id(lease_id)
        )
        obj_key = self._object_key("leases", f"{effective_id}.json")

        self._assert_no_live_gc_fence(
            materialised_chunks, now_epoch=now, when="before the lease write"
        )
        preflight = self._chunk_generations(materialised_chunks)

        if self._pre_lease_put_race_probe is not None:  # test seam, see __init__
            self._pre_lease_put_race_probe()

        head_resp = self._send("HEAD", object_key=obj_key)
        if head_resp.status_code == 200:
            raise VaultRevisionConflictError(f"S3 push lease {effective_id!r} already exists")
        if head_resp.status_code != 404:
            raise VaultTransportError(
                f"S3 lease probe failed for {effective_id} with HTTP {head_resp.status_code}"
            )
        our_lease_token = uuid.uuid4().hex
        payload = _build_lease_json_bytes(
            lease_id=effective_id,
            project_id=project_id,
            now_epoch=now,
            ttl_seconds=finite_ttl,
            chunk_ids=materialised_chunks,
            lease_token=our_lease_token,
        )
        put_resp = self._send(
            "PUT",
            object_key=obj_key,
            body=payload,
            content_type="application/json",
            extra_headers={"if-none-match": "*"},
        )
        if put_resp.status_code in (409, 412):
            raise VaultRevisionConflictError(f"S3 push lease {effective_id!r} collision")
        if put_resp.status_code not in (200, 201, 204):
            raise VaultTransportError(
                f"S3 acquire_push_lease failed with HTTP {put_resp.status_code}"
            )
        # Identity of *our* write. Without it nothing downstream can tell our lease
        # from a successor's, so a backend that reports no ETag is a hard failure.
        our_write_etag = self._lease_write_etag(obj_key, put_resp, our_lease_token)
        if not our_write_etag:
            raise VaultTransportError(
                f"S3 acquire_push_lease could not establish an ETag for lease {effective_id!r}; "
                "refusing to continue without a way to undo this write conditionally"
            )
        try:
            self._assert_no_live_gc_fence(
                materialised_chunks, now_epoch=now, when="after the lease write"
            )
        except VaultRevisionConflictError:
            # Roll our own object back before propagating: a lease that outlived the
            # refusal would protect a chunk the collector is already deleting. The
            # rollback is ownership-bound and conditional — see _rollback_lease_acquire.
            self._rollback_lease_acquire(
                obj_key, our_write_etag=our_write_etag, our_lease_token=our_lease_token
            )
            raise
        lost = self._lost_materialised_chunks(preflight)
        if lost:
            self._rollback_lease_acquire(
                obj_key, our_write_etag=our_write_etag, our_lease_token=our_lease_token
            )
            raise self._lost_chunk_error(lost, "after the lease write")
        try:
            self._assert_no_live_gc_fence(
                materialised_chunks, now_epoch=now, when="after the chunk existence proof"
            )
        except VaultRevisionConflictError:
            self._rollback_lease_acquire(
                obj_key, our_write_etag=our_write_etag, our_lease_token=our_lease_token
            )
            raise
        self._reclaim_dead_gc_fences(materialised_chunks, now_epoch=now)
        # From here on this instance is the owner of the object it wrote: a later
        # ``release_push_lease(lease_id)`` can condition its delete on *this* ETag,
        # so a refresh by another writer makes our release a 412 instead of a DELETE.
        self._owned_lease_etags[effective_id] = our_write_etag
        self._owned_lease_tokens[effective_id] = our_lease_token
        return effective_id

    @staticmethod
    def _lease_stem_from_key(obj_key: str) -> str:
        """``leases/<stem>.json`` -> ``<stem>``; the key is the only handle a rollback has."""
        return obj_key.rsplit("/", 1)[-1].removesuffix(".json")

    def _forget_owned_lease(self, obj_key: str) -> None:
        """Drop this instance's ownership record for the lease object at ``obj_key``."""
        lease_id = self._lease_stem_from_key(obj_key)
        self._owned_lease_etags.pop(lease_id, None)
        self._owned_lease_tokens.pop(lease_id, None)

    def _rollback_lease_refresh(
        self,
        obj_key: str,
        *,
        prior_body: bytes | None,
        prior_etag: str,
        our_write_etag: str,
        our_lease_token: str,
    ) -> None:
        """Undo a refresh that raced a collector — only while it is still *our* write.

        Every step is a proof rather than an assumption:

        1. no ``our_write_etag`` means the bucket never told us which object our own
           ``PUT`` created; an unconditional ``DELETE``/``PUT`` here would be exactly
           the bug this method exists to prevent, so nothing happens at all;
        2. the object is read back, and the rollback aborts unless the current ETag is
           still the one our write returned *and* the body still carries our
           ``lease_token``. A refresher or a third writer that replaced the object in
           between wins by definition;
        3. the ``PUT`` (restore) or ``DELETE`` (no prior body) is sent with
           ``If-Match: our_write_etag``, so the check in step 2 and the write are one
           atomic decision at the store. A 409/412 there means the state moved again
           and the rollback simply stops — never retried, never forced.

        A ``prior_body`` that itself names a chunk which just disappeared is still
        restored: deleting it would remove a lease this caller no longer owns. The
        caller raises the conflict either way, and the lease expires on its own TTL.

        Whatever happens, this instance stops claiming ownership of the object: the
        write it made is being undone, so a later ``release_push_lease`` must observe
        the object again instead of trusting a stale ETag.
        """
        self._forget_owned_lease(obj_key)
        if not our_write_etag:
            return
        try:
            state = self._read_lease_state(obj_key)
        except VaultTransportError:
            return
        if state is None:
            return
        state_lease_id, state_project_id, _state_verifier = _lease_owner_fields(state.raw)
        if not _lease_state_matches_token(
            state,
            etag=our_write_etag,
            project_id=state_project_id,
            lease_id=state_lease_id,
            lease_token=our_lease_token,
        ):
            return
        if prior_body is not None and prior_etag and state.etag == prior_etag:
            return
        if prior_body is not None:
            resp = self._send(
                "PUT",
                object_key=obj_key,
                body=prior_body,
                content_type="application/json",
                extra_headers={"if-match": our_write_etag},
            )
        else:
            resp = self._send(
                "DELETE",
                object_key=obj_key,
                extra_headers={"if-match": our_write_etag},
            )
        if resp.status_code in (200, 201, 202, 204, 404, 409, 412):
            return
        raise VaultTransportError(
            f"S3 lease rollback failed for {obj_key} with HTTP {resp.status_code}"
        )

    def _reconcile_ambiguous_lease_refresh(
        self,
        obj_key: str,
        *,
        lease_id: str,
        project_id: str,
        our_lease_token: str,
        prior_etag: str,
        prior_lease_verifier: str,
    ) -> str:
        """Prove an ambiguously acknowledged PUT using only its verifier and owner IDs."""
        try:
            state = self._read_lease_state(obj_key)
        except (VaultTransportError, OSError, TimeoutError) as exc:
            raise VaultTransportError(
                f"Ambiguous S3 lease refresh for {lease_id!r}; reconciliation read failed"
            ) from exc
        if state is None or not state.etag:
            raise VaultTransportError(
                f"Ambiguous S3 lease refresh for {lease_id!r}; no verifiable remote version"
            )
        try:
            _parse_and_validate_lease_payload(state.raw, source_label=obj_key)
        except VaultTransportError as exc:
            raise VaultTransportError(
                f"Ambiguous S3 lease refresh for {lease_id!r}; remote lease is invalid"
            ) from exc
        remote_id, remote_project, remote_verifier = _lease_owner_fields(state.raw)
        if remote_id != lease_id or remote_project != project_id:
            raise VaultRevisionConflictError(
                f"S3 lease {lease_id!r} changed owner during ambiguous refresh"
            )
        expected_verifier = _compute_lease_token_verifier(project_id, lease_id, our_lease_token)
        try:
            ours = bool(remote_verifier) and hmac.compare_digest(expected_verifier, remote_verifier)
            prior_is_still_present = (
                bool(prior_lease_verifier)
                and state.etag == prior_etag
                and hmac.compare_digest(remote_verifier, prior_lease_verifier)
            )
        except TypeError:
            ours = False
            prior_is_still_present = False
        if ours:
            return state.etag
        if prior_is_still_present:
            raise VaultTransportError(
                f"S3 update_push_lease for {lease_id!r} was not committed; "
                "the prior version remains"
            )
        raise VaultRevisionConflictError(
            f"S3 lease {lease_id!r} was replaced by another writer during ambiguous refresh"
        )

    def update_push_lease(
        self,
        lease_id: str,
        chunk_ids: Iterable[str],
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
        expected_etag: str | None = None,
    ) -> bool:
        """Refresh only a live remote version owned by this instance's token and ETag."""
        valid_id = _validate_snapshot_id(lease_id)
        owned = _owned_lease_pair(
            self._owned_lease_tokens, self._owned_lease_etags, valid_id, expected_etag
        )
        if owned is None:
            state = self._read_lease_state(self._object_key("leases", f"{valid_id}.json"))
            if state is None:
                raise VaultTransportError(f"S3 lease {valid_id!r} is absent")
            raise VaultRevisionConflictError(
                f"This driver cannot prove ownership of lease {valid_id!r}"
            )
        owned_token, owned_etag = owned
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        materialised_chunks = [_validate_chunk_id(cid) for cid in chunk_ids]
        obj_key = self._object_key("leases", f"{valid_id}.json")
        now = self._clock().timestamp()

        self._assert_no_live_gc_fence(
            materialised_chunks, now_epoch=now, when="before the lease refresh"
        )
        preflight = self._chunk_generations(materialised_chunks)
        if self._pre_lease_put_race_probe is not None:
            self._pre_lease_put_race_probe()

        head_resp = self._send("HEAD", object_key=obj_key)
        if head_resp.status_code == 404:
            self._forget_owned_lease(obj_key)
            return False
        if head_resp.status_code != 200:
            raise VaultTransportError(
                f"S3 lease probe failed for {valid_id} with HTTP {head_resp.status_code}"
            )
        lowered_head = {k.lower(): v for k, v in head_resp.headers.items()}
        head_etag = lowered_head.get("etag", "").strip()
        prior = self._send("GET", object_key=obj_key)
        if prior.status_code == 404:
            self._forget_owned_lease(obj_key)
            return False
        if prior.status_code != 200 or not prior.body:
            raise VaultTransportError(
                f"S3 update_push_lease could not read the prior lease {valid_id!r} "
                f"(HTTP {prior.status_code})"
            )
        get_etag = {k.lower(): v for k, v in (prior.headers or {}).items()}.get("etag", "").strip()
        if not head_etag or not get_etag:
            raise VaultTransportError("S3 lease update requires a non-empty ETag precondition")
        if head_etag != get_etag:
            raise VaultRevisionConflictError(
                f"S3 push lease {valid_id!r} changed between HEAD and GET"
            )
        actual_id, actual_project, _verifier = _lease_owner_fields(prior.body)
        if (
            get_etag != owned_etag
            or actual_id != valid_id
            or (project_id and actual_project != project_id)
            or not _lease_record_matches_owner(
                prior.body,
                project_id=actual_project,
                lease_id=valid_id,
                lease_token=owned_token,
            )
        ):
            self._forget_owned_lease(obj_key)
            return False
        try:
            expires_at, _prior_chunks = _parse_and_validate_lease_payload(
                prior.body, source_label=obj_key
            )
        except VaultTransportError:
            self._forget_owned_lease(obj_key)
            return False
        if _lease_deadline_epoch(prior.body, prior.headers, expires_at) <= now:
            return False

        prior_etag = get_etag
        prior_body = prior.body
        prior_verifier = _parse_lease_token_verifier(prior_body)
        our_lease_token = uuid.uuid4().hex
        payload = _build_lease_json_bytes(
            lease_id=valid_id,
            project_id=actual_project,
            now_epoch=now,
            ttl_seconds=finite_ttl,
            chunk_ids=materialised_chunks,
            lease_token=our_lease_token,
        )
        conditional: dict[str, str] = {"if-match": prior_etag}
        reconciled_etag: str | None = None
        try:
            put_resp: S3HttpResponse | None = self._send(
                "PUT",
                object_key=obj_key,
                body=payload,
                content_type="application/json",
                extra_headers=conditional,
            )
        except (VaultAuthenticationError, VaultQuotaExceededError):
            raise
        except (VaultTransportError, OSError, TimeoutError):
            reconciled_etag = self._reconcile_ambiguous_lease_refresh(
                obj_key,
                lease_id=valid_id,
                project_id=actual_project,
                our_lease_token=our_lease_token,
                prior_etag=prior_etag,
                prior_lease_verifier=prior_verifier,
            )
            put_resp = None

        if put_resp is not None:
            if put_resp.status_code in (409, 412):
                raise VaultRevisionConflictError(
                    f"Concurrent modification of S3 lease {valid_id!r} during refresh"
                )
            if put_resp.status_code in (200, 201, 204):
                reconciled_etag = self._lease_write_etag(obj_key, put_resp, our_lease_token)
            elif put_resp.status_code in (408, 425, 429) or 500 <= put_resp.status_code < 600:
                reconciled_etag = self._reconcile_ambiguous_lease_refresh(
                    obj_key,
                    lease_id=valid_id,
                    project_id=actual_project,
                    our_lease_token=our_lease_token,
                    prior_etag=prior_etag,
                    prior_lease_verifier=prior_verifier,
                )
            else:
                raise VaultTransportError(
                    f"S3 update_push_lease failed with HTTP {put_resp.status_code}"
                )

        our_write_etag = reconciled_etag or ""
        if not our_write_etag:
            raise VaultTransportError(
                f"S3 update_push_lease could not establish an ETag for {valid_id!r}"
            )
        try:
            self._assert_no_live_gc_fence(
                materialised_chunks, now_epoch=now, when="after the lease refresh"
            )
        except VaultRevisionConflictError:
            self._rollback_lease_refresh(
                obj_key,
                prior_body=prior_body,
                prior_etag=prior_etag,
                our_write_etag=our_write_etag,
                our_lease_token=our_lease_token,
            )
            raise
        lost = self._lost_materialised_chunks(preflight)
        if lost:
            self._rollback_lease_refresh(
                obj_key,
                prior_body=prior_body,
                prior_etag=prior_etag,
                our_write_etag=our_write_etag,
                our_lease_token=our_lease_token,
            )
            raise self._lost_chunk_error(lost, "after the lease refresh")
        try:
            self._assert_no_live_gc_fence(
                materialised_chunks, now_epoch=now, when="after the chunk existence proof"
            )
        except VaultRevisionConflictError:
            self._rollback_lease_refresh(
                obj_key,
                prior_body=prior_body,
                prior_etag=prior_etag,
                our_write_etag=our_write_etag,
                our_lease_token=our_lease_token,
            )
            raise
        self._owned_lease_etags[valid_id] = our_write_etag
        self._owned_lease_tokens[valid_id] = our_lease_token
        return True

    def get_owned_lease_token(self, lease_id: str) -> str:
        """Return a private credential only after re-proving its current owner pair."""
        return self.export_lease_credential(lease_id)["lease_token"]

    def export_lease_credential(self, lease_id: str) -> dict[str, str]:
        """Export the current credential only after proving the remote token/ETag pair."""
        valid_id = _validate_snapshot_id(lease_id)
        owned = _owned_lease_pair(self._owned_lease_tokens, self._owned_lease_etags, valid_id, None)
        if owned is None:
            raise VaultDriverError(f"This driver does not own S3 lease {valid_id!r}")
        token, owned_etag = owned
        obj_key = self._object_key("leases", f"{valid_id}.json")
        state = self._read_lease_state(obj_key)
        if state is None:
            raise VaultDriverError(f"S3 lease {valid_id!r} is absent")
        actual_id, project_id, _verifier = _lease_owner_fields(state.raw)
        if (
            state.etag != owned_etag
            or actual_id != valid_id
            or not _lease_record_matches_owner(
                state.raw, project_id=project_id, lease_id=valid_id, lease_token=token
            )
        ):
            raise VaultDriverError(f"S3 lease {valid_id!r} is no longer owned here")
        return {
            "lease_id": valid_id,
            "project_id": project_id,
            "lease_token": token,
            "etag": state.etag,
        }

    def recover_owned_lease(
        self,
        project_id: str,
        lease_id: str,
        lease_token: str,
        expected_etag: str | None = None,
    ) -> str:
        """Recover only an active remote lease whose one-way owner verifier matches."""
        valid_id = _validate_snapshot_id(lease_id)
        if not isinstance(lease_token, str) or not lease_token:
            raise VaultDriverError("Lease recovery requires a non-empty owner token")
        obj_key = self._object_key("leases", f"{valid_id}.json")
        state = self._read_lease_state(obj_key)
        if state is None or not state.etag:
            raise VaultDriverError(f"S3 lease {valid_id!r} is absent or has no ETag")
        expires_at, _chunks = _parse_and_validate_lease_payload(state.raw, source_label=obj_key)
        if (
            state.deadline_epoch is not None and state.deadline_epoch <= self._clock().timestamp()
        ) or expires_at <= self._clock().timestamp():
            raise VaultDriverError(f"S3 lease {valid_id!r} has expired")
        actual_verifier = _lease_verifier_for_recovery(
            state.raw,
            project_id=project_id,
            lease_id=valid_id,
            source_label=obj_key,
        )
        expected_verifier = _compute_lease_token_verifier(project_id, valid_id, lease_token)
        if not hmac.compare_digest(expected_verifier, actual_verifier):
            raise VaultDriverError(f"S3 lease {valid_id!r} owner token did not verify")
        if (
            not isinstance(expected_etag, str)
            or not expected_etag
            or not hmac.compare_digest(expected_etag, state.etag)
        ):
            raise VaultDriverError(f"S3 lease {valid_id!r} ETag did not verify")
        self._owned_lease_tokens[valid_id] = lease_token
        self._owned_lease_etags[valid_id] = state.etag
        return state.etag

    def release_push_lease(self, lease_id: str, *, expected_etag: str | None = None) -> bool:
        """Release only a current remote lease proved by this instance's token and ETag."""
        valid_id = _validate_snapshot_id(lease_id)
        owned = _owned_lease_pair(
            self._owned_lease_tokens, self._owned_lease_etags, valid_id, expected_etag
        )
        if owned is None:
            state = self._read_lease_state(self._object_key("leases", f"{valid_id}.json"))
            if state is None:
                return False
            raise VaultRevisionConflictError(
                f"This driver cannot prove ownership of lease {valid_id!r}"
            )
        token, owned_etag = owned
        obj_key = self._object_key("leases", f"{valid_id}.json")
        state = self._read_lease_state(obj_key)
        if state is None:
            self._forget_owned_lease(obj_key)
            return False
        actual_id, project_id, _verifier = _lease_owner_fields(state.raw)
        if (
            state.etag != owned_etag
            or actual_id != valid_id
            or not _lease_record_matches_owner(
                state.raw, project_id=project_id, lease_id=valid_id, lease_token=token
            )
        ):
            self._forget_owned_lease(obj_key)
            raise VaultRevisionConflictError(
                f"Lease {valid_id!r} changed owner or ETag before release"
            )
        resp = self._send(
            "DELETE",
            object_key=obj_key,
            extra_headers={"if-match": owned_etag},
        )
        if resp.status_code in (409, 412, 404):
            self._forget_owned_lease(obj_key)
            raise VaultRevisionConflictError(
                f"Lease {valid_id!r} changed owner or ETag before release"
            )
        if resp.status_code not in (200, 202, 204):
            raise VaultTransportError(
                f"S3 release_push_lease failed for {valid_id} with HTTP {resp.status_code}"
            )
        self._forget_owned_lease(obj_key)
        return True

    def read_active_leased_chunks(self, *, now_epoch: float | None = None) -> set[str]:
        """List and parse all active ``leases/*.json`` objects fail-closed."""
        effective_now = _validate_finite_epoch(now_epoch, default_now=self._clock().timestamp())
        active_chunks: set[str] = set()
        for stem in self._list_category_stems("leases", ".json"):
            obj_key = self._object_key("leases", f"{stem}.json")
            resp = self._send("GET", object_key=obj_key)
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                raise VaultTransportError(
                    f"S3 read_active_leased_chunks failed for {stem} with HTTP {resp.status_code}"
                )
            expires_at, chunk_set = _parse_and_validate_lease_payload(
                resp.body,
                source_label=obj_key,
            )
            # A lease is only debris when it is provably older than its own deadline
            # *plus* the leeway, measured on the later of the writer's clock estimate
            # and the bucket's Last-Modified + ttl. A writer whose clock lags must not
            # be able to have its brand-new lease reaped — that is the same window as
            # the fence ABA race, arriving through the clock instead of the network.
            deadline = _lease_deadline_epoch(resp.body, resp.headers, expires_at)
            if deadline + self._clock_skew_margin <= effective_now:
                lowered = {k.lower(): v for k, v in resp.headers.items()}
                # Reaping expired debris must never be an unconditional DELETE: the
                # writer may have refreshed (or another writer taken over) the object
                # between the listing GET and the delete. Only a lease that is still
                # byte-identical to what was just read — and still expired — is debris.
                reaped = self._reap_expired_lease_if_unchanged(
                    obj_key,
                    expected_etag=lowered.get("etag", "").strip(),
                    expected_lease_verifier=_parse_lease_token_verifier(resp.body),
                    effective_now=effective_now,
                )
                if not reaped:
                    # Unproven state: keep its chunks protected for this pass.
                    active_chunks.update(chunk_set)
                continue
            active_chunks.update(chunk_set)
        return active_chunks

    def _read_lease_state(self, obj_key: str) -> _LeaseObjectState | None:
        """Read one lease object, carrying its ETag, owner verifier and absolute deadline.

        ``deadline_epoch`` is ``None`` when the document cannot be parsed; every caller
        treats that as "not provably expired" / "not provably ours" and leaves the
        object alone.
        """
        resp = self._send("GET", object_key=obj_key)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 lease state read failed for {obj_key} with HTTP {resp.status_code}"
            )
        lowered = {k.lower(): v for k, v in resp.headers.items()}
        deadline: float | None = None
        with contextlib.suppress(VaultTransportError):
            expires_at, _names = _parse_and_validate_lease_payload(resp.body, source_label=obj_key)
            deadline = _lease_deadline_epoch(resp.body, resp.headers, expires_at)
        return _LeaseObjectState(
            raw=resp.body,
            etag=lowered.get("etag", "").strip(),
            lease_token_verifier=_parse_lease_token_verifier(resp.body),
            deadline_epoch=deadline,
        )

    def _lease_write_etag(
        self,
        obj_key: str,
        put_resp: S3HttpResponse,
        lease_token: str,
    ) -> str:
        """ETag of the lease object our own ``PUT`` created, or ``""`` (fail-closed).

        Prefers the ``ETag`` on the write response. Backends that do not echo it get
        one read-back, accepted only when the body carries *our* token — an object
        that is not provably our write must never be treated as one, because every
        rollback and every reap is conditioned on this value.
        """
        lowered = {k.lower(): v for k, v in put_resp.headers.items()}
        etag = lowered.get("etag", "").strip()
        if etag:
            return etag
        state = self._read_lease_state(obj_key)
        if state is None or not state.etag:
            return ""
        lease_id, project_id, _verifier = _lease_owner_fields(state.raw)
        if not _lease_state_matches_token(
            state,
            etag=state.etag,
            project_id=project_id,
            lease_id=lease_id,
            lease_token=lease_token,
        ):
            return ""
        return state.etag

    def _reap_expired_lease_if_unchanged(
        self,
        obj_key: str,
        *,
        expected_etag: str,
        expected_lease_verifier: str,
        effective_now: float,
    ) -> bool:
        """Delete an expired lease only while it is provably still the one we read.

        Returns ``True`` exactly when the object is gone and was ours to remove.
        ``False`` means "the state moved or cannot be proven" — a refresher renewed the
        deadline, a third writer replaced the object, the bucket refused the
        precondition, or no ETag was ever observed. The caller must then keep that
        lease's chunks in its active set: a chunk deleted on an ambiguous lease state
        is not recoverable, while a chunk kept alive costs one GC pass.
        """
        if not expected_etag:
            return False
        try:
            state = self._read_lease_state(obj_key)
        except VaultTransportError:
            return False
        if state is None:
            return True
        if state.deadline_epoch is None:
            return False
        if not state.is_ours(etag=expected_etag, lease_token_verifier=expected_lease_verifier):
            return False
        if state.deadline_epoch + self._clock_skew_margin > effective_now:
            return False
        try:
            resp = self._send(
                "DELETE",
                object_key=obj_key,
                extra_headers={"if-match": expected_etag},
            )
        except VaultTransportError:
            return False
        if resp.status_code in (200, 202, 204):
            return True
        if resp.status_code == 404:
            return True
        if resp.status_code in (409, 412):
            return False
        raise VaultTransportError(
            f"S3 expired-lease reap failed for {obj_key} with HTTP {resp.status_code}"
        )

    def _rollback_lease_acquire(
        self,
        obj_key: str,
        *,
        our_write_etag: str,
        our_lease_token: str,
    ) -> None:
        """Remove the lease object this acquire just created — and nothing else.

        Same rule as :meth:`_rollback_lease_refresh`: no ETag means no delete, the
        observation must still be our own write (ETag *and* token), and the ``DELETE``
        is sent with ``If-Match`` so a lease another writer took over is never removed.
        """
        self._forget_owned_lease(obj_key)
        if not our_write_etag:
            return
        try:
            state = self._read_lease_state(obj_key)
        except VaultTransportError:
            return
        if state is None:
            return
        state_lease_id, state_project_id, _state_verifier = _lease_owner_fields(state.raw)
        if not _lease_state_matches_token(
            state,
            etag=our_write_etag,
            project_id=state_project_id,
            lease_id=state_lease_id,
            lease_token=our_lease_token,
        ):
            return
        resp = self._send(
            "DELETE",
            object_key=obj_key,
            extra_headers={"if-match": our_write_etag},
        )
        if resp.status_code in (200, 202, 204, 404, 409, 412):
            return
        raise VaultTransportError(
            f"S3 lease rollback failed for {obj_key} with HTTP {resp.status_code}"
        )

    def _lease_state_token(self) -> tuple[tuple[str, str], ...]:
        """Return a comparable ``(stem, ETag|LastModified)`` snapshot of lease objects."""
        state: list[tuple[str, str]] = []
        for stem in self._list_category_stems("leases", ".json"):
            head_resp = self._send("HEAD", object_key=self._object_key("leases", f"{stem}.json"))
            if head_resp.status_code == 404:
                state.append((stem, "absent"))
                continue
            if head_resp.status_code != 200:
                raise VaultTransportError(
                    f"S3 lease state probe failed for {stem} with HTTP {head_resp.status_code}"
                )
            lowered = {k.lower(): v for k, v in head_resp.headers.items()}
            state.append((stem, f"{lowered.get('etag', '')}|{lowered.get('last-modified', '')}"))
        return tuple(sorted(state))

    def _read_gc_fence(self, chunk_id: str) -> _GcFence | None:
        """Read the fence guarding ``chunk_id``; ``None`` when the bucket has none."""
        resp = self._send("GET", object_key=self._gc_fence_key(chunk_id))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 GC fence probe failed for {chunk_id} with HTTP {resp.status_code}"
            )
        lowered = {k.lower(): v for k, v in resp.headers.items()}
        return _parse_gc_fence(
            resp.body,
            lowered.get("etag"),
            observed_at_epoch=_server_object_epoch(resp.headers),
        )

    def _live_gc_fenced_chunks(
        self, chunk_ids: Iterable[str], *, now_epoch: float
    ) -> tuple[str, ...]:
        """Return the chunk ids currently guarded by a *live* collector fence."""
        fenced: list[str] = []
        for cid in chunk_ids:
            fence = self._read_gc_fence(cid)
            if fence is not None and fence.is_live(
                now_epoch, margin_seconds=self._clock_skew_margin
            ):
                fenced.append(cid)
        return tuple(fenced)

    def _create_gc_fence(self, chunk_id: str, *, now_epoch: float) -> tuple[str, str | None] | None:
        """Claim the fence for ``chunk_id``; ``None`` means somebody else holds it.

        The claim is created with ``If-None-Match: *`` so two collectors can never
        both believe they own a chunk — the loser gets 409/412 and simply does not
        collect. Only a *provably expired* claim may be taken over, and only with an
        ``If-Match`` on the exact bytes that were inspected, so a live collector is
        never silently overwritten.
        """
        fence_key = self._gc_fence_key(chunk_id)
        token = uuid.uuid4().hex
        body = _build_gc_fence_body(token, chunk_id, now_epoch=now_epoch)
        resp = self._send(
            "PUT",
            object_key=fence_key,
            body=body,
            content_type="application/json",
            extra_headers={"if-none-match": "*"},
        )
        if resp.status_code in (409, 412):
            existing = self._read_gc_fence(chunk_id)
            if (
                existing is None
                or existing.is_live(now_epoch, margin_seconds=self._clock_skew_margin)
                or not existing.etag
            ):
                return None
            reclaimed = self._send(
                "PUT",
                object_key=fence_key,
                body=body,
                content_type="application/json",
                extra_headers={"if-match": existing.etag},
            )
            if reclaimed.status_code in (409, 412):
                return None
            if reclaimed.status_code not in (200, 201, 204):
                raise VaultTransportError(
                    f"S3 GC fence reclaim failed for {chunk_id} with HTTP {reclaimed.status_code}"
                )
            return token, reclaimed.headers.get("etag") or reclaimed.headers.get("ETag")
        if resp.status_code not in (200, 201, 204):
            raise VaultTransportError(
                f"S3 GC fence claim failed for {chunk_id} with HTTP {resp.status_code}"
            )
        return token, resp.headers.get("etag") or resp.headers.get("ETag")

    def _fence_token_is_ours(self, chunk_id: str, token: str) -> bool:
        """True only while the bucket still carries *our* claim, byte for byte."""
        fence = self._read_gc_fence(chunk_id)
        if fence is None or not fence.token:
            return False
        return hmac.compare_digest(fence.token, token)

    def _retire_gc_fence(self, chunk_id: str, token: str) -> bool:
        """Delete our fence — never somebody else's.

        The owner token is re-read immediately before the delete, and the delete
        itself is conditional on the observed ``ETag`` when the bucket reports one.
        A fence that was replaced in between is left alone: it belongs to a newer
        collector whose claim we have no right to erase.
        """
        fence = self._read_gc_fence(chunk_id)
        if fence is None:
            return True
        if not fence.token or not hmac.compare_digest(fence.token, token):
            return False
        if not fence.etag:
            # No ETag means no conditional DELETE is possible, and an unconditional
            # DELETE could destroy a *newer* owner's claim that landed one round trip
            # ago. Leaving the fence in place costs a writer a retry until the TTL
            # expires; deleting the wrong fence costs data. Fail closed.
            return False
        resp = self._send(
            "DELETE",
            object_key=self._gc_fence_key(chunk_id),
            extra_headers={"if-match": fence.etag},
        )
        if resp.status_code in (409, 412):
            return False
        if resp.status_code not in (200, 202, 204, 404):
            raise VaultTransportError(
                f"S3 GC fence retire failed for {chunk_id} with HTTP {resp.status_code}"
            )
        return True

    def _delete_gc_fence_if_unchanged(self, chunk_id: str, observed: _GcFence) -> bool:
        """Delete a *dead* fence only if it still holds exactly the bytes we read."""
        current = self._read_gc_fence(chunk_id)
        if current is None:
            return True
        if current.raw != observed.raw:
            return False
        if not current.etag:
            # Same rule as :meth:`_retire_gc_fence`: byte equality in this process is
            # not an ETag, and a backend that cannot answer conditionally cannot
            # prove the object is still the one we inspected. Refuse the delete.
            return False
        resp = self._send(
            "DELETE",
            object_key=self._gc_fence_key(chunk_id),
            extra_headers={"if-match": current.etag},
        )
        if resp.status_code in (409, 412):
            return False
        if resp.status_code not in (200, 202, 204, 404):
            raise VaultTransportError(
                f"S3 GC fence cleanup failed for {chunk_id} with HTTP {resp.status_code}"
            )
        return True

    def _restore_chunk_conditionally(self, chunk_id: str, staged_payload: bytes) -> bool:
        """Put staged bytes back without ever overwriting a newer object.

        Two independent guarantees, because a rollback that corrupts is worse than a
        rollback that fails:

        * **identity** — if the staged bytes are an AEAD wire envelope, the chunk id
          authenticated in its header must equal the object key. A payload filed
          under the wrong id is refused outright (:class:`VaultTamperDetectedError`)
          instead of being written back under a lie;
        * **no clobbering** — the write is create-if-absent (``If-None-Match: *``).
          S3 answers 409/412 when a concurrent push already republished the chunk,
          and that object is authoritative: the collector verifies it exists and
          leaves it untouched. The restored object is then read back and compared
          by SHA-256, so "restored" means "the bytes are actually there".
        """
        embedded = _wire_envelope_chunk_id(staged_payload)
        if embedded is not None and embedded != chunk_id:
            raise VaultTamperDetectedError(
                f"Refusing to restore chunk {chunk_id}: staged wire envelope is {embedded}"
            )
        expected_digest = hashlib.sha256(staged_payload).hexdigest()
        resp = self._send(
            "PUT",
            object_key=self._object_key("chunks", f"{chunk_id}.chk"),
            body=staged_payload,
            content_type="application/octet-stream",
            extra_headers={
                "if-none-match": "*",
                CHUNK_GENERATION_META_HEADER: _new_chunk_generation(),
            },
        )
        if resp.status_code in (409, 412):
            # Somebody republished this chunk while we were rolling back. Their object
            # wins by contract; we only prove the chunk is not lost.
            return self.has_chunk(chunk_id)
        if resp.status_code not in (200, 201, 204):
            raise VaultTransportError(
                f"S3 GC chunk restore failed for {chunk_id} with HTTP {resp.status_code}"
            )
        self._chunk_mtimes[chunk_id] = self._clock().timestamp()
        readback = self._send("GET", object_key=self._object_key("chunks", f"{chunk_id}.chk"))
        if readback.status_code != 200:
            return False
        return hashlib.sha256(readback.body).hexdigest() == expected_digest

    def _final_lease_fence_check(
        self,
        chunk_id: str,
        *,
        expected_state: tuple[tuple[str, str], ...],
        now_epoch: float,
    ) -> bool:
        """Return ``True`` only if the lease world is still exactly ``expected_state``.

        This is the last observation before the fence object is retired. The two-sided
        fence already brackets the chunk ``DELETE``, but the final fence ``DELETE`` and
        the post-delete re-read are still separated by one round trip: a lease created
        inside that gap would protect a chunk this call had already removed, and would
        be written against a fence that is about to disappear. Fail-closed here — any
        change in the lease state token, or any active lease covering ``chunk_id``,
        keeps the fence in place and makes the caller roll the bytes back.
        """
        if self._lease_state_token() != expected_state:
            return False
        return chunk_id not in self.read_active_leased_chunks(now_epoch=now_epoch)

    def delete_chunk_if_unleased(
        self,
        chunk_id: str,
        *,
        now_epoch: float | None = None,
    ) -> bool:
        """Delete ``chunk_id`` only when no lease can claim it, across processes.

        The collector and every push run as *independent* processes against one
        bucket, so nothing here may rest on ``self._coord_lock`` (that lock only
        orders calls inside one interpreter). The protocol is therefore two-phase
        and object-based, in the sense of Dekker's handshake:

        1. the collector **claims** ``gc-fences/<chunk>.fence`` with
           ``If-None-Match: *`` and a body carrying a unique owner token plus an
           expiry — two collectors can never both own a chunk;
        2. the collector reads the lease set, deletes the chunk, and re-reads both
           the fence *and* the leases twice more;
        3. the fence is retired only while the collector still owns it, and the
           lease set is read **once more after the fence is gone**.

        That last read is what closes the millisecond window between the final check
        and the fence delete: a pusher that registers a lease there is still seen
        (its lease outlives the fence), so the collector puts the bytes back and
        reports ``False``. Symmetrically, a pusher refuses to lease a chunk whose
        fence is live, and re-reads fences *after* writing its lease (see
        :meth:`acquire_push_lease`). Both sides write their claim before reading the
        other's, so no interleaving lets both of them miss.

        Every rollback goes through :meth:`_restore_chunk_conditionally`: identity
        is checked against the object key and the write is create-if-absent, so a
        concurrent push is never overwritten. Any result other than ``True`` means
        the chunk is still (or again) present.
        """
        valid = _validate_chunk_id(chunk_id)
        effective_now = _validate_finite_epoch(now_epoch, default_now=self._clock().timestamp())
        with self._coord_lock:
            before_state = self._lease_state_token()
            if valid in self.read_active_leased_chunks(now_epoch=effective_now):
                return False
            if self._lease_state_token() != before_state:
                return False
            if not self.has_chunk(valid):
                return False
            if self._lease_state_token() != before_state:
                return False

            staged_payload = self.get_chunk(valid)
            claim = self._create_gc_fence(valid, now_epoch=effective_now)
            if claim is None:
                # Another collector owns the chunk right now (or a live fence cannot
                # be proven dead). Refusing is the only safe answer.
                return False
            fence_token, _ = claim
            retired = False
            try:
                if not self._fence_token_is_ours(valid, fence_token):
                    return False
                if self._lease_state_token() != before_state or valid in (
                    self.read_active_leased_chunks(now_epoch=effective_now)
                ):
                    return False

                self.delete_chunk(valid)

                if (
                    not self._fence_token_is_ours(valid, fence_token)
                    or self._lease_state_token() != before_state
                    or valid in self.read_active_leased_chunks(now_epoch=effective_now)
                ):
                    self._restore_chunk_conditionally(valid, staged_payload)
                    return False

                if self._fence_race_probe is not None:  # test seam, see __init__
                    self._fence_race_probe()
                if not self._final_lease_fence_check(
                    valid, expected_state=before_state, now_epoch=effective_now
                ):
                    self._restore_chunk_conditionally(valid, staged_payload)
                    return False

                if self._post_final_check_race_probe is not None:  # test seam
                    self._post_final_check_race_probe()
                # The check above is a round trip old. Re-verify ownership *and* the
                # lease world at the instant of retirement: this is the window the
                # audit found, and it is now observed twice more instead of once.
                if not self._fence_token_is_ours(valid, fence_token) or not (
                    self._final_lease_fence_check(
                        valid, expected_state=before_state, now_epoch=effective_now
                    )
                ):
                    self._restore_chunk_conditionally(valid, staged_payload)
                    return False

                retired = self._retire_gc_fence(valid, fence_token)
                if not retired:
                    # Our claim was replaced while we were retiring it: the deletion
                    # is no longer ours to publish.
                    self._restore_chunk_conditionally(valid, staged_payload)
                    return False

                # Final, post-retirement read. The fence is gone now, so *this* is
                # the only observation that can still see a lease registered in the
                # last round trip — the exact race the audit described.
                if self._lease_state_token() != before_state or valid in (
                    self.read_active_leased_chunks(now_epoch=effective_now)
                ):
                    self._restore_chunk_conditionally(valid, staged_payload)
                    return False
                return True
            finally:
                if not retired:
                    # A claim we own must never outlive this call: it would block
                    # every writer for a whole TTL. Retirement is idempotent and
                    # refuses to touch a fence that is no longer ours.
                    self._retire_gc_fence(valid, fence_token)

    def _staging_journal_key(self, project_id: str, parent_snapshot_id: str | None) -> str:
        digest = _staging_journal_digest(project_id, parent_snapshot_id)
        return self._object_key("staging", f"stage-{digest}.json")

    def save_staging_journal(self, journal: VaultStagingJournal) -> None:
        """Persist a crash-recovery staging journal object on the S3 bucket."""
        obj_key = self._staging_journal_key(journal.project_id, journal.parent_snapshot_id)
        resp = self._send(
            "PUT",
            object_key=obj_key,
            body=journal.to_json_bytes(),
            content_type="application/json",
        )
        if resp.status_code not in (200, 201, 204):
            raise VaultTransportError(
                f"S3 save_staging_journal failed with HTTP {resp.status_code}"
            )

    def load_staging_journal(
        self,
        project_id: str,
        parent_snapshot_id: str | None,
    ) -> VaultStagingJournal | None:
        """Load (and self-heal) the crash-recovery staging journal object, if any."""
        obj_key = self._staging_journal_key(project_id, parent_snapshot_id)
        resp = self._send("GET", object_key=obj_key)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 load_staging_journal failed with HTTP {resp.status_code}"
            )
        try:
            journal = VaultStagingJournal.from_json_bytes(resp.body)
        except (UnicodeDecodeError, ValueError, KeyError, TypeError, PersonalVaultError):
            self.clear_staging_journal(project_id, parent_snapshot_id)
            return None
        if journal.project_id != project_id or journal.parent_snapshot_id != parent_snapshot_id:
            self.clear_staging_journal(project_id, parent_snapshot_id)
            return None
        return journal

    def clear_staging_journal(self, project_id: str, parent_snapshot_id: str | None) -> None:
        """Delete the crash-recovery staging journal object after a completed push."""
        obj_key = self._staging_journal_key(project_id, parent_snapshot_id)
        with contextlib.suppress(VaultTransportError):
            self._send("DELETE", object_key=obj_key)


# --------------------------------------------------------------------------- #
# 3. PortableBundleVaultDriver (.nagarvault single-file binary archive)
# --------------------------------------------------------------------------- #

#: ``.nagarvault`` archive format version written by this module.
#:
#: Version-compatibility contract
#: ------------------------------
#: * ``BUNDLE_VERSION == 1`` is the only accepted version. The header stores a
#:   ``u32`` version alongside the magic ``NAGARVLT``; any other value raises
#:   :class:`VaultTamperDetectedError` ("unsupported version"), so a bundle written
#:   by a newer writer is rejected rather than silently mis-parsed.
#: * Version 1 defines exactly four entry kinds in the index table:
#:   ``1 == _ENTRY_CHUNK`` (key = chunk id), ``2 == _ENTRY_MANIFEST``
#:   (key = snapshot id), ``3 == _ENTRY_LEASE`` (key = ``leases/<id>.json``) and
#:   ``4 == _ENTRY_STAGING`` (key = ``staging/stage-<digest>.json``).
#:   Because version 1 always carried the ``_ENTRY_LEASE`` kind, older readers that
#:   predate lease support must reject lease entries instead of ignoring them —
#:   ignoring a lease entry would silently invalidate the fail-closed lease check
#:   that protects in-flight chunks from GC. Unknown kinds raise
#:   :class:`VaultTamperDetectedError`, never a skip. Adding a *new* kind is therefore
#:   a ``BUNDLE_VERSION`` bump.
#: * Entry ordering inside the archive is canonical and irrelevant to readers
#:   (chunks, then manifests, then leases, each sorted); a reader must accept any
#:   ordering because only per-entry CRC32/SHA-256 plus the archive-level SHA-256
#:   are authoritative.
BUNDLE_MAGIC = b"NAGARVLT"
BUNDLE_VERSION = 1
BUNDLE_SUFFIX = ".nagarvault"
_BUNDLE_HEADER_PREFIX = struct.Struct(">8sHIQQ")  # 30 bytes
_BUNDLE_HEADER_TOTAL = _BUNDLE_HEADER_PREFIX.size + 32  # + 32B archive sha256 = 62 bytes
_BUNDLE_INDEX_HEAD = struct.Struct(">BH")
_BUNDLE_INDEX_TAIL = struct.Struct(">QQI32s")
_ENTRY_CHUNK = 1
_ENTRY_MANIFEST = 2
_ENTRY_LEASE = 3
_ENTRY_STAGING = 4
_STAGING_ENTRY_KEY_RE = re.compile(r"^staging/stage-[0-9a-f]{32}\.json$")


class PortableBundleVaultDriver:
    """Integrity-verified single-file ``.nagarvault`` backend.

    Writes stream archive segments; loads hash through a fixed 64 KiB buffer, parse
    the compact index, and read one blob at a time. Because the bundle is self-contained,
    retained storage is necessarily ``O(sum(S_chunk))``; this backend cost is separate
    from the manager's ``O(S_chunk)`` transient push-payload bound.
    """

    __slots__ = (
        "_bundle_path",
        "_max_quota_bytes",
        "_chunks",
        "_manifests",
        "_leases",
        "_staging_journals",
        "_chunk_mtimes",
        "_owned_lease_etags",
        "_owned_lease_tokens",
        "_lock_path",
        "_owner_path",
    )

    def __init__(
        self,
        bundle_path: Path | str,
        *,
        max_quota_bytes: int | None = None,
    ) -> None:
        raw_path = Path(bundle_path)
        if raw_path.suffix != BUNDLE_SUFFIX:
            raise PersonalVaultError(
                f"Portable bundle path must end with {BUNDLE_SUFFIX!r}, got {raw_path.name!r}"
            )
        if max_quota_bytes is not None and max_quota_bytes <= 0:
            raise PersonalVaultError(
                f"max_quota_bytes must be positive when set, got {max_quota_bytes}"
            )
        if raw_path.is_symlink():
            raise VaultTransportError(f"Bundle path must not be a symlink: {raw_path}")

        self._bundle_path = raw_path.resolve()
        self._max_quota_bytes = max_quota_bytes
        self._chunks: dict[str, bytes] = {}
        self._manifests: dict[str, bytes] = {}
        self._leases: dict[str, bytes] = {}
        self._staging_journals: dict[str, bytes] = {}
        self._chunk_mtimes: dict[str, float] = {}
        # Private in-memory ownership credentials, never stored in the shared bundle.
        # Release binds both token and ETag so a peer cannot adopt observed state.
        self._owned_lease_etags: dict[str, str] = {}
        self._owned_lease_tokens: dict[str, str] = {}
        # Cross-process lock identities live next to (never inside) the archive.
        self._lock_path = self._bundle_path.with_name(f".{self._bundle_path.name}.lock")
        self._owner_path = self._bundle_path.with_name(f".{self._bundle_path.name}.owner")

        try:
            self._bundle_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VaultTransportError(f"Cannot create bundle parent directory: {exc}") from exc

        if self._bundle_path.exists():
            self._load_and_verify()

    @property
    def bundle_path(self) -> Path:
        return self._bundle_path

    @contextlib.contextmanager
    def _coordination_lock(
        self,
        *,
        timeout_seconds: float = 10.0,
        stale_lock_seconds: float = 30.0,
    ) -> Iterator[None]:
        """Serialize every archive read-modify-write across processes.

        Two ``PortableBundleVaultDriver`` instances (or two processes) sharing one
        ``.nagarvault`` file would otherwise clobber each other: the archive is a single
        all-or-nothing blob, so an unserialized writer would resurrect chunks another
        writer just deleted, drop a peer's push lease, and turn the lease-check +
        ``delete_chunk`` pair into a TOCTOU race. The lock is mandatory for every
        mutation and for the lease-then-delete decision.
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _cross_process_file_lock(
            self._lock_path,
            owner_path=self._owner_path,
            timeout_seconds=timeout_seconds,
            stale_lock_seconds=stale_lock_seconds,
        ):
            yield

    def _reload_locked(self) -> None:
        """Refresh in-memory state from disk while holding the coordination lock."""
        if not self._bundle_path.exists():
            self._chunks = {}
            self._manifests = {}
            self._leases = {}
            self._staging_journals = {}
            self._chunk_mtimes = {}
            return
        self._load_and_verify()

    def verify_bundle(self) -> None:
        """Verify binary header, index bounds, per-blob CRC32/SHA-256, and archive SHA-256."""
        self._load_and_verify()

    def _load_and_verify(self) -> None:
        """Verify and load the archive with O(chunk) peak RAM, not O(archive).

        The file is never slurped into one ``bytes``: the header is read first, the
        archive-level SHA-256 is computed by streaming the remainder through a fixed
        64 KiB scratch buffer, the (metadata-sized) index is read and parsed, and each
        blob is then ``seek``-ed to and read individually. Peak load memory is therefore
        one chunk plus the index, instead of ``raw`` (the whole archive) *plus* a second
        full copy of every chunk via ``raw[offset:offset + len]`` slicing.
        """
        if self._bundle_path.is_symlink():
            raise VaultTransportError(f"Bundle file does not exist: {self._bundle_path}")
        if _FCNTL is not None and not hasattr(os, "O_NONBLOCK"):
            raise VaultTransportError(
                "POSIX platform cannot safely open bundles without O_NONBLOCK"
            )
        flags = os.O_RDONLY
        for flag_name in ("O_NONBLOCK", "O_NOFOLLOW", "O_CLOEXEC"):
            if hasattr(os, flag_name):
                flags |= getattr(os, flag_name)
        try:
            fd = os.open(self._bundle_path, flags)
        except OSError as exc:
            raise VaultTransportError(f"Cannot read bundle {self._bundle_path}: {exc}") from exc
        handle: Any | None = None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise VaultTransportError(f"Bundle is not a regular file: {self._bundle_path}")
            try:
                before_read = os.stat(self._bundle_path, follow_symlinks=False)
            except OSError as exc:
                raise VaultRevisionConflictError(
                    f"Bundle {self._bundle_path} changed while it was being opened"
                ) from exc
            if (opened.st_dev, opened.st_ino) != (before_read.st_dev, before_read.st_ino):
                raise VaultRevisionConflictError(
                    f"Bundle {self._bundle_path} changed while it was being opened"
                )
            handle = os.fdopen(fd, "rb")
            fd = -1
            self._verify_and_load_from_handle(handle)
            try:
                after_read = os.stat(self._bundle_path, follow_symlinks=False)
            except OSError as exc:
                raise VaultRevisionConflictError(
                    f"Bundle {self._bundle_path} changed while it was being read"
                ) from exc
            if (opened.st_dev, opened.st_ino) != (after_read.st_dev, after_read.st_ino):
                raise VaultRevisionConflictError(
                    f"Bundle {self._bundle_path} changed while it was being read"
                )
        except OSError as exc:
            raise VaultTransportError(f"Cannot read bundle {self._bundle_path}: {exc}") from exc
        finally:
            if handle is not None:
                handle.close()
            if fd >= 0:
                os.close(fd)

    def _verify_and_load_from_handle(self, handle: _StreamingBinaryReader) -> None:
        """Streaming body of :meth:`_load_and_verify` over an already-open ``rb`` handle."""
        import zlib

        file_size = os.fstat(handle.fileno()).st_size
        header = _read_exact(handle, _BUNDLE_HEADER_TOTAL, what="bundle header")
        header_prefix = header[: _BUNDLE_HEADER_PREFIX.size]
        stored_archive_sha = header[_BUNDLE_HEADER_PREFIX.size : _BUNDLE_HEADER_TOTAL]
        magic, version, entry_count, index_offset, index_len = _BUNDLE_HEADER_PREFIX.unpack(
            header_prefix
        )
        if magic != BUNDLE_MAGIC or version != BUNDLE_VERSION:
            raise VaultTamperDetectedError("Invalid .nagarvault magic or unsupported version")

        if (
            index_offset < _BUNDLE_HEADER_TOTAL
            or index_offset > file_size
            or index_offset + index_len != file_size
        ):
            raise VaultTamperDetectedError("Corrupted .nagarvault binary index bounds")

        # Archive-level SHA-256: header prefix + everything after the digest, streamed
        # through one reusable 64 KiB buffer so the file is never fully resident.
        archive_hasher = hashlib.sha256()
        archive_hasher.update(header_prefix)
        handle.seek(_BUNDLE_HEADER_TOTAL)
        scratch = bytearray(_BUNDLE_VERIFY_BLOCK_BYTES)
        scratch_view = memoryview(scratch)
        try:
            while True:
                read_len = handle.readinto(scratch)
                if not read_len:
                    break
                archive_hasher.update(scratch_view[:read_len])
        finally:
            scratch_view.release()
            del scratch
        if not hmac.compare_digest(archive_hasher.digest(), stored_archive_sha):
            raise VaultTamperDetectedError(
                "Archive-level SHA-256 mismatch in .nagarvault bundle: file is corrupted"
            )

        # The index is metadata-only (one record per entry), so it is read in one piece.
        handle.seek(index_offset)
        index_bytes = _read_exact(handle, index_len, what="bundle index")

        loaded_chunks: dict[str, bytes] = {}
        loaded_manifests: dict[str, bytes] = {}
        loaded_leases: dict[str, bytes] = {}
        loaded_staging: dict[str, bytes] = {}
        archive_mtime = self._bundle_path.stat().st_mtime
        cursor = 0

        for _ in range(entry_count):
            if cursor + _BUNDLE_INDEX_HEAD.size > index_len:
                raise VaultTamperDetectedError("Truncated entry header in .nagarvault binary index")
            entry_kind, key_len = _BUNDLE_INDEX_HEAD.unpack_from(index_bytes, cursor)
            cursor += _BUNDLE_INDEX_HEAD.size

            if cursor + key_len + _BUNDLE_INDEX_TAIL.size > index_len:
                raise VaultTamperDetectedError("Truncated entry metadata in .nagarvault index")
            try:
                key = index_bytes[cursor : cursor + key_len].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise VaultTamperDetectedError("Invalid UTF-8 key in .nagarvault index") from exc
            cursor += key_len

            blob_offset, blob_len, expected_crc32, expected_sha256 = _BUNDLE_INDEX_TAIL.unpack_from(
                index_bytes, cursor
            )
            cursor += _BUNDLE_INDEX_TAIL.size

            if (
                blob_offset < _BUNDLE_HEADER_TOTAL
                or blob_offset + blob_len > index_offset
                or blob_len < 0
            ):
                raise VaultTamperDetectedError("Out-of-bounds payload slice in .nagarvault index")

            # Only this one blob is resident at a time (``blob_len`` is one chunk).
            handle.seek(blob_offset)
            blob = _read_exact(handle, blob_len, what="bundle payload entry")
            actual_crc32 = zlib.crc32(blob) & 0xFFFFFFFF
            actual_sha256 = hashlib.sha256(blob).digest()
            if actual_crc32 != expected_crc32 or not hmac.compare_digest(
                actual_sha256, expected_sha256
            ):
                raise VaultTamperDetectedError(
                    f"Entry checksum mismatch in .nagarvault bundle for {key!r}"
                )

            if entry_kind == _ENTRY_CHUNK:
                _validate_chunk_id(key)
                loaded_chunks[key] = blob
            elif entry_kind == _ENTRY_MANIFEST:
                _validate_snapshot_id(key)
                loaded_manifests[key] = blob
            elif entry_kind == _ENTRY_LEASE:
                if not key.startswith("leases/") or not key.endswith(".json"):
                    raise VaultTamperDetectedError(f"Malformed lease key in .nagarvault: {key!r}")
                stem = key[len("leases/") : -len(".json")]
                _validate_snapshot_id(stem)
                loaded_leases[stem] = blob
            elif entry_kind == _ENTRY_STAGING:
                if not _STAGING_ENTRY_KEY_RE.fullmatch(key):
                    raise VaultTamperDetectedError(f"Malformed staging key in .nagarvault: {key!r}")
                loaded_staging[key] = blob
            else:
                raise VaultTamperDetectedError(f"Unknown .nagarvault entry kind: {entry_kind}")

        if cursor != index_len:
            raise VaultTamperDetectedError("Trailing unparsed bytes in .nagarvault binary index")

        self._chunks = loaded_chunks
        self._manifests = loaded_manifests
        self._leases = loaded_leases
        self._staging_journals = loaded_staging
        for cid in list(self._chunk_mtimes):
            if cid not in loaded_chunks:
                del self._chunk_mtimes[cid]
        for cid in loaded_chunks:
            self._chunk_mtimes.setdefault(cid, archive_mtime)

    def _build_archive_layout(
        self,
        chunks: Mapping[str, bytes],
        manifests: Mapping[str, bytes],
        leases: Mapping[str, bytes],
        staging: Mapping[str, bytes] | None = None,
    ) -> _ArchiveLayout:
        """Compute the binary index and header without concatenating the payload."""
        import zlib

        ordered_entries: list[tuple[int, str, bytes]] = []
        for chunk_id in sorted(chunks):
            ordered_entries.append((_ENTRY_CHUNK, chunk_id, chunks[chunk_id]))
        for snapshot_id in sorted(manifests):
            ordered_entries.append((_ENTRY_MANIFEST, snapshot_id, manifests[snapshot_id]))
        for lease_id in sorted(leases):
            ordered_entries.append((_ENTRY_LEASE, f"leases/{lease_id}.json", leases[lease_id]))
        staging_map = staging or {}
        for staging_key in sorted(staging_map):
            ordered_entries.append((_ENTRY_STAGING, staging_key, staging_map[staging_key]))

        payload_parts: list[bytes] = []
        index_parts: list[bytes] = []
        current_offset = _BUNDLE_HEADER_TOTAL

        for kind, key, blob in ordered_entries:
            key_bytes = key.encode("utf-8")
            blob_len = len(blob)
            crc = zlib.crc32(blob) & 0xFFFFFFFF
            sha = hashlib.sha256(blob).digest()
            payload_parts.append(blob)
            index_parts.append(
                _BUNDLE_INDEX_HEAD.pack(kind, len(key_bytes))
                + key_bytes
                + _BUNDLE_INDEX_TAIL.pack(current_offset, blob_len, crc, sha)
            )
            current_offset += blob_len

        index_bytes = b"".join(index_parts)
        header_prefix = _BUNDLE_HEADER_PREFIX.pack(
            BUNDLE_MAGIC,
            BUNDLE_VERSION,
            len(ordered_entries),
            current_offset,
            len(index_bytes),
        )
        return _ArchiveLayout(
            header_prefix=header_prefix,
            payload_parts=tuple(payload_parts),
            index_bytes=index_bytes,
        )

    def _serialize_archive(
        self,
        chunks: Mapping[str, bytes],
        manifests: Mapping[str, bytes],
        leases: Mapping[str, bytes],
        staging: Mapping[str, bytes] | None = None,
    ) -> bytes:
        """Return the full archive bytes via a single pre-sized buffer."""
        return self._build_archive_layout(chunks, manifests, leases, staging).to_bytes()

    def _flush(
        self,
        next_chunks: dict[str, bytes],
        next_manifests: dict[str, bytes],
        next_leases: dict[str, bytes],
        next_staging: Mapping[str, bytes],
    ) -> None:
        layout = self._build_archive_layout(next_chunks, next_manifests, next_leases, next_staging)
        total_size = layout.total_size
        if self._max_quota_bytes is not None and total_size > self._max_quota_bytes:
            raise VaultQuotaExceededError(
                f"Portable bundle size {total_size} exceeds max_quota_bytes {self._max_quota_bytes}"
            )
        # Stream the archive straight to the temp file: the payload is never gathered
        # into a second in-memory copy of the whole vault.
        _atomic_write_stream(self._bundle_path, layout.iter_segments())
        self._chunks = next_chunks
        self._manifests = next_manifests
        self._leases = next_leases
        self._staging_journals = dict(next_staging)

    def _read_locked(self) -> None:
        """Reload from disk under the shared lock so readers see peer writes."""
        with self._coordination_lock():
            self._reload_locked()

    def _mutate(
        self,
        mutator: Callable[[dict[str, bytes], dict[str, bytes], dict[str, bytes]], None],
    ) -> None:
        """Reload, mutate, and persist the archive atomically under the shared lock."""
        with self._coordination_lock():
            self._reload_locked()
            next_chunks = dict(self._chunks)
            next_manifests = dict(self._manifests)
            next_leases = dict(self._leases)
            mutator(next_chunks, next_manifests, next_leases)
            self._flush(next_chunks, next_manifests, next_leases, dict(self._staging_journals))

    def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
        valid = _validate_chunk_id(chunk_id)
        payload = bytes(data)
        stamp = time.time()

        def _apply(
            chunks: dict[str, bytes],
            manifests: dict[str, bytes],
            leases: dict[str, bytes],
        ) -> None:
            del manifests, leases
            chunks[valid] = payload

        self._mutate(_apply)
        self._chunk_mtimes[valid] = stamp

    def get_chunk(self, chunk_id: str) -> bytes:
        valid = _validate_chunk_id(chunk_id)
        self._read_locked()
        if valid not in self._chunks:
            raise VaultTransportError(f"Chunk not found in .nagarvault bundle: {valid}")
        return self._chunks[valid]

    def has_chunk(self, chunk_id: str) -> bool:
        valid = _validate_chunk_id(chunk_id)
        self._read_locked()
        return valid in self._chunks

    def get_chunk_mtime(self, chunk_id: str) -> float | None:
        """Return POSIX mtime recorded for ``chunk_id`` inside this bundle, or ``None``."""
        valid = _validate_chunk_id(chunk_id)
        self._read_locked()
        if valid not in self._chunks:
            return None
        return self._chunk_mtimes.get(valid)

    def put_manifest(self, snapshot_id: str, data: bytes) -> None:
        valid = _validate_snapshot_id(snapshot_id)
        payload = bytes(data)

        def _apply(
            chunks: dict[str, bytes],
            manifests: dict[str, bytes],
            leases: dict[str, bytes],
        ) -> None:
            del chunks, leases
            manifests[valid] = payload

        self._mutate(_apply)

    def get_manifest(self, snapshot_id: str) -> bytes:
        valid = _validate_snapshot_id(snapshot_id)
        self._read_locked()
        if valid not in self._manifests:
            raise VaultTransportError(f"Manifest not found in .nagarvault bundle: {valid}")
        return self._manifests[valid]

    def list_manifests(self) -> list[str]:
        self._read_locked()
        return sorted(self._manifests)

    def list_chunks(self) -> list[str]:
        """Return sorted chunk IDs stored inside this ``.nagarvault`` bundle."""
        self._read_locked()
        return sorted(self._chunks)

    def delete_chunk(self, chunk_id: str) -> None:
        valid = _validate_chunk_id(chunk_id)

        def _apply(
            chunks: dict[str, bytes],
            manifests: dict[str, bytes],
            leases: dict[str, bytes],
        ) -> None:
            del manifests, leases
            chunks.pop(valid, None)

        self._mutate(_apply)
        self._chunk_mtimes.pop(valid, None)

    def _staging_journal_key(self, project_id: str, parent_snapshot_id: str | None) -> str:
        digest = _staging_journal_digest(project_id, parent_snapshot_id)
        return f"staging/stage-{digest}.json"

    def save_staging_journal(self, journal: VaultStagingJournal) -> None:
        """Persist a crash-recovery staging journal entry inside this bundle."""
        key = self._staging_journal_key(journal.project_id, journal.parent_snapshot_id)
        payload = journal.to_json_bytes()
        with self._coordination_lock():
            self._reload_locked()
            next_staging = dict(self._staging_journals)
            next_staging[key] = payload
            self._flush(
                dict(self._chunks),
                dict(self._manifests),
                dict(self._leases),
                next_staging,
            )

    def load_staging_journal(
        self,
        project_id: str,
        parent_snapshot_id: str | None,
    ) -> VaultStagingJournal | None:
        """Load (and self-heal) this bundle's crash-recovery staging journal, if any."""
        key = self._staging_journal_key(project_id, parent_snapshot_id)
        self._read_locked()
        raw = self._staging_journals.get(key)
        if raw is None:
            return None
        try:
            journal = VaultStagingJournal.from_json_bytes(raw)
        except (UnicodeDecodeError, ValueError, KeyError, TypeError, PersonalVaultError):
            self.clear_staging_journal(project_id, parent_snapshot_id)
            return None
        if journal.project_id != project_id or journal.parent_snapshot_id != parent_snapshot_id:
            self.clear_staging_journal(project_id, parent_snapshot_id)
            return None
        return journal

    def clear_staging_journal(self, project_id: str, parent_snapshot_id: str | None) -> None:
        """Drop the crash-recovery staging journal entry after a completed push."""
        key = self._staging_journal_key(project_id, parent_snapshot_id)
        with self._coordination_lock():
            self._reload_locked()
            if key not in self._staging_journals:
                return
            next_staging = dict(self._staging_journals)
            del next_staging[key]
            self._flush(
                dict(self._chunks),
                dict(self._manifests),
                dict(self._leases),
                next_staging,
            )

    def acquire_push_lease(
        self,
        chunk_ids: Iterable[str] = (),
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
        lease_id: str | None = None,
    ) -> str:
        """Record a ``leases/<lease_id>.json`` entry inside this ``.nagarvault`` bundle."""
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        leased_ids = sorted({_validate_chunk_id(cid) for cid in chunk_ids})
        now = time.time()
        effective_id = (
            f"{os.getpid()}-{int(now * 1000)}-{secrets.token_hex(6)}"
            if lease_id is None
            else _validate_snapshot_id(lease_id)
        )
        bundle_lease_token = uuid.uuid4().hex
        payload = _build_lease_json_bytes(
            lease_id=effective_id,
            project_id=project_id,
            now_epoch=now,
            ttl_seconds=finite_ttl,
            chunk_ids=leased_ids,
            lease_token=bundle_lease_token,
        )

        def _apply(
            chunks: dict[str, bytes],
            manifests: dict[str, bytes],
            leases: dict[str, bytes],
        ) -> None:
            del chunks, manifests
            if effective_id in self._leases:
                raise VaultRevisionConflictError(
                    f"Portable bundle lease {effective_id!r} already exists"
                )
            leases[effective_id] = payload

        self._mutate(_apply)
        self._owned_lease_etags[effective_id] = hashlib.sha256(payload).hexdigest()
        self._owned_lease_tokens[effective_id] = bundle_lease_token
        return effective_id

    def update_push_lease(
        self,
        lease_id: str,
        chunk_ids: Iterable[str],
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
        expected_etag: str | None = None,
    ) -> bool:
        """Refresh only a current bundle lease owned by this token and ETag pair."""
        valid_id = _validate_snapshot_id(lease_id)
        owned = _owned_lease_pair(
            self._owned_lease_tokens, self._owned_lease_etags, valid_id, expected_etag
        )
        if owned is None:
            with self._coordination_lock():
                self._reload_locked()
                current = self._leases.get(valid_id)
            if current is None:
                raise VaultTransportError(f"Bundle lease {valid_id!r} is absent")
            raise VaultRevisionConflictError(
                f"This driver cannot prove ownership of lease {valid_id!r}"
            )
        owned_token, owned_etag = owned
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        leased_ids = sorted({_validate_chunk_id(cid) for cid in chunk_ids})
        bundle_refresh_token = uuid.uuid4().hex
        now = time.time()
        with self._coordination_lock():
            self._reload_locked()
            current = self._leases.get(valid_id)
            if current is None:
                self._owned_lease_tokens.pop(valid_id, None)
                self._owned_lease_etags.pop(valid_id, None)
                raise VaultRevisionConflictError(
                    f"Lease {valid_id!r} changed owner or ETag before refresh"
                )
            current_etag = hashlib.sha256(current).hexdigest()
            actual_id, actual_project, _verifier = _lease_owner_fields(current)
            if (
                current_etag != owned_etag
                or actual_id != valid_id
                or (project_id and actual_project != project_id)
                or not _lease_record_matches_owner(
                    current,
                    project_id=actual_project,
                    lease_id=valid_id,
                    lease_token=owned_token,
                )
            ):
                self._owned_lease_tokens.pop(valid_id, None)
                self._owned_lease_etags.pop(valid_id, None)
                raise VaultRevisionConflictError(
                    f"Lease {valid_id!r} changed owner or ETag before refresh"
                )
            expires_at, _current_chunks = _parse_and_validate_lease_payload(
                current, source_label=f"bundle://leases/{valid_id}.json"
            )
            if expires_at <= now:
                return False
            payload = _build_lease_json_bytes(
                lease_id=valid_id,
                project_id=actual_project,
                now_epoch=now,
                ttl_seconds=finite_ttl,
                chunk_ids=leased_ids,
                lease_token=bundle_refresh_token,
            )
            next_leases = dict(self._leases)
            next_leases[valid_id] = payload
            self._flush(
                dict(self._chunks),
                dict(self._manifests),
                next_leases,
                dict(self._staging_journals),
            )
        self._owned_lease_etags[valid_id] = hashlib.sha256(payload).hexdigest()
        self._owned_lease_tokens[valid_id] = bundle_refresh_token
        return True

    def get_owned_lease_token(self, lease_id: str) -> str:
        """Return a private credential only after re-proving its current owner pair."""
        return self.export_lease_credential(lease_id)["lease_token"]

    def export_lease_credential(self, lease_id: str) -> dict[str, str]:
        """Export the current private credential after checking the shared lease verifier."""
        valid_id = _validate_snapshot_id(lease_id)
        owned = _owned_lease_pair(self._owned_lease_tokens, self._owned_lease_etags, valid_id, None)
        if owned is None:
            raise VaultDriverError(f"This driver does not own bundle lease {valid_id!r}")
        token, owned_etag = owned
        with self._coordination_lock():
            self._reload_locked()
            raw = self._leases.get(valid_id)
            if raw is None:
                raise VaultDriverError(f"Bundle lease {valid_id!r} is absent")
            current_etag = hashlib.sha256(raw).hexdigest()
            actual_id, project_id, _verifier = _lease_owner_fields(raw)
            if (
                current_etag != owned_etag
                or actual_id != valid_id
                or not _lease_record_matches_owner(
                    raw, project_id=project_id, lease_id=valid_id, lease_token=token
                )
            ):
                raise VaultDriverError(f"Bundle lease {valid_id!r} is no longer owned here")
        return {
            "lease_id": valid_id,
            "project_id": project_id,
            "lease_token": token,
            "etag": current_etag,
        }

    def recover_owned_lease(
        self,
        project_id: str,
        lease_id: str,
        lease_token: str,
        expected_etag: str | None = None,
    ) -> str:
        """Recover only an active bundle lease whose one-way verifier proves the token."""
        valid_id = _validate_snapshot_id(lease_id)
        if not isinstance(lease_token, str) or not lease_token:
            raise VaultDriverError("Lease recovery requires a non-empty owner token")
        with self._coordination_lock():
            self._reload_locked()
            raw = self._leases.get(valid_id)
            if raw is None:
                raise VaultDriverError(f"Bundle lease {valid_id!r} is absent")
            expires_at, _chunks = _parse_and_validate_lease_payload(
                raw, source_label=f"bundle://leases/{valid_id}.json"
            )
            if expires_at <= time.time():
                raise VaultDriverError(f"Bundle lease {valid_id!r} has expired")
            verifier = _lease_verifier_for_recovery(
                raw,
                project_id=project_id,
                lease_id=valid_id,
                source_label=f"bundle://leases/{valid_id}.json",
            )
            expected_verifier = _compute_lease_token_verifier(project_id, valid_id, lease_token)
            if not hmac.compare_digest(expected_verifier, verifier):
                raise VaultDriverError(f"Bundle lease {valid_id!r} owner token did not verify")
            current_etag = hashlib.sha256(raw).hexdigest()
            if (
                not isinstance(expected_etag, str)
                or not expected_etag
                or not hmac.compare_digest(expected_etag, current_etag)
            ):
                raise VaultDriverError(f"Bundle lease {valid_id!r} ETag did not verify")
            self._owned_lease_tokens[valid_id] = lease_token
            self._owned_lease_etags[valid_id] = current_etag
            return current_etag

    def release_push_lease(self, lease_id: str, *, expected_etag: str | None = None) -> bool:
        """Release only a bundle lease proven by this instance's token and exact ETag."""
        valid_id = _validate_snapshot_id(lease_id)
        owned = _owned_lease_pair(
            self._owned_lease_tokens, self._owned_lease_etags, valid_id, expected_etag
        )
        if owned is None:
            with self._coordination_lock():
                self._reload_locked()
                present = valid_id in self._leases
            if not present:
                return False
            raise VaultRevisionConflictError(
                f"This driver cannot prove ownership of lease {valid_id!r}"
            )
        token, owned_etag = owned
        with self._coordination_lock():
            self._reload_locked()
            current = self._leases.get(valid_id)
            if current is None:
                self._owned_lease_tokens.pop(valid_id, None)
                self._owned_lease_etags.pop(valid_id, None)
                return False
            current_etag = hashlib.sha256(current).hexdigest()
            actual_id, project_id, _verifier = _lease_owner_fields(current)
            if (
                current_etag != owned_etag
                or actual_id != valid_id
                or not _lease_record_matches_owner(
                    current, project_id=project_id, lease_id=valid_id, lease_token=token
                )
            ):
                self._owned_lease_tokens.pop(valid_id, None)
                self._owned_lease_etags.pop(valid_id, None)
                return False
            next_leases = dict(self._leases)
            next_leases.pop(valid_id, None)
            self._flush(
                dict(self._chunks),
                dict(self._manifests),
                next_leases,
                dict(self._staging_journals),
            )
            self._owned_lease_tokens.pop(valid_id, None)
            self._owned_lease_etags.pop(valid_id, None)
        return True

    def _active_leases_unlocked(self, *, effective_now: float) -> tuple[set[str], list[str]]:
        active_chunks: set[str] = set()
        expired_ids: list[str] = []
        for stem, raw_bytes in sorted(self._leases.items()):
            expires_at, chunk_set = _parse_and_validate_lease_payload(
                raw_bytes,
                source_label=f"bundle://leases/{stem}.json",
            )
            if expires_at <= effective_now:
                expired_ids.append(stem)
                continue
            active_chunks.update(chunk_set)
        return active_chunks, expired_ids

    def read_active_leased_chunks(self, *, now_epoch: float | None = None) -> set[str]:
        """Read unexpired ``leases/*.json`` entries fail-closed from this bundle."""
        effective_now = _validate_finite_epoch(now_epoch, default_now=time.time())
        with self._coordination_lock():
            self._reload_locked()
            active_chunks, expired_ids = self._active_leases_unlocked(effective_now=effective_now)
            if expired_ids:
                expired = set(expired_ids)
                updated_leases = {k: v for k, v in self._leases.items() if k not in expired}
                self._flush(
                    dict(self._chunks),
                    dict(self._manifests),
                    updated_leases,
                    dict(self._staging_journals),
                )
        return active_chunks

    def delete_chunk_if_unleased(
        self,
        chunk_id: str,
        *,
        now_epoch: float | None = None,
    ) -> bool:
        """Verify bundle leases and delete ``chunk_id`` atomically under the bundle lock.

        Both the lease re-read and the deletion happen inside one critical section, so a
        concurrent ``acquire_push_lease``/``update_push_lease`` (which must take the same
        lock) can never register a claim between the lease check and the deletion.
        """
        valid = _validate_chunk_id(chunk_id)
        effective_now = _validate_finite_epoch(now_epoch, default_now=time.time())
        with self._coordination_lock():
            self._reload_locked()
            active_chunks, expired_ids = self._active_leases_unlocked(effective_now=effective_now)
            next_leases = dict(self._leases)
            for stem in expired_ids:
                del next_leases[stem]
            if valid in active_chunks or valid not in self._chunks:
                if expired_ids:
                    self._flush(
                        dict(self._chunks),
                        dict(self._manifests),
                        next_leases,
                        dict(self._staging_journals),
                    )
                return False
            next_chunks = dict(self._chunks)
            del next_chunks[valid]
            self._flush(
                next_chunks,
                dict(self._manifests),
                next_leases,
                dict(self._staging_journals),
            )
            self._chunk_mtimes.pop(valid, None)
            return True


def export_snapshot_to_bundle(
    source_driver: BYOSStorageDriver,
    snapshot_id: str,
    bundle_path: Path | str,
) -> PortableBundleVaultDriver:
    """Package a single encrypted snapshot and all its chunks into a ``.nagarvault`` file."""
    manifest_bytes = source_driver.get_manifest(snapshot_id)
    manifest = VaultSnapshotManifest.from_json_bytes(manifest_bytes)
    bundle = PortableBundleVaultDriver(bundle_path)
    for chunk_id in sorted(manifest.referenced_chunk_ids()):
        chunk_bytes = source_driver.get_chunk(chunk_id)
        bundle.put_chunk(chunk_id, chunk_bytes)
    bundle.put_manifest(snapshot_id, manifest_bytes)
    bundle.verify_bundle()
    return bundle


__all__ = [
    "BUNDLE_MAGIC",
    "BUNDLE_SUFFIX",
    "BUNDLE_VERSION",
    "BYOSStorageDriver",
    "HttpTransportCallable",
    "LocalDeviceVaultDriver",
    "PortableBundleVaultDriver",
    "S3CompatibleSigV4Driver",
    "S3HttpRequest",
    "S3HttpResponse",
    "S3SigV4Config",
    "SigV4Artifacts",
    "VaultStagingJournal",
    "compute_sigv4_signature",
    "export_snapshot_to_bundle",
]
