"""Final lease-fence proof for ``S3CompatibleSigV4Driver.delete_chunk_if_unleased``.

Audit item 2/1 of PR #350: the two-sided fence protected the chunk ``DELETE``,
but the *fence object* was still retired on the strength of an observation taken
one round trip earlier. A lease created inside that gap is the only race the
fence protocol could not see — the chunk was already gone, and the lease that
should have protected it was about to be written against a fence that no longer
existed.

The driver now takes one last authoritative lease listing before destroying the
fence (``_final_lease_fence_check``). This module lands the race deterministically
through the documented test seam (``_fence_race_probe``) and asserts the
fail-closed outcome: ``False``, bytes restored, fence retired, lease honoured.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from nexus_ai_agent.storage.personal_cloud_vault import (
    S3CompatibleSigV4Driver,
    S3HttpRequest,
    S3HttpResponse,
    S3SigV4Config,
    VaultRevisionConflictError,
    VaultTransportError,
)
from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import (
    _compute_lease_token_verifier,
)
from nexus_ai_agent.storage.personal_cloud_vault.errors import VaultTamperDetectedError

CHUNK = "c" * 64
PREFIX = "user-7"
BUCKET = "fence-bkt"
LEASE_KEY = f"{PREFIX}/leases/newcomer.json"
FENCE_KEY = f"{PREFIX}/gc-fences/{CHUNK}.fence"
CHUNK_KEY = f"{PREFIX}/chunks/{CHUNK}.chk"


class _LeaseRaceTransport:
    """Minimal in-memory S3 REST surface: LIST/HEAD/GET/PUT/DELETE on objects."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.meta: dict[str, dict[str, str]] = {}
        self.version_ids: dict[str, str] = {}
        self._writes = 0

    def _stamp(self, key: str) -> None:
        """Advance the object version: every write mints a new version id."""
        self._writes += 1
        self.version_ids[key] = f"v{self._writes}"

    def put_lease(self, expires_at_epoch: float) -> None:
        self._stamp(LEASE_KEY)
        self.objects[LEASE_KEY] = json.dumps(
            {
                "lease_id": "newcomer",
                "project_id": "",
                "lease_token_verifier": _compute_lease_token_verifier(
                    "", "newcomer", "foreign-writer-token"
                ),
                "expires_at_epoch": expires_at_epoch,
                "chunk_ids": [CHUNK],
            }
        ).encode()

    def _headers_for(self, key: str) -> dict[str, str]:
        """ETag, Last-Modified and version id for ``key`` — what a bucket returns.

        The fence protocol retires an object only with ``If-Match``, so a fake that
        omitted the ETag would silently test a driver that had downgraded to an
        unconditional ``DELETE``. The version id is the same argument one level
        further: chunk identity is ``(version, generation, ETag)``, and a fake that
        reported none of them would make the ABA proof unreachable rather than
        tested. Modelling the headers is part of modelling S3.
        """
        body = self.objects[key]
        headers = {
            "ETag": '"' + hashlib.sha256(body).hexdigest()[:32] + '"',
            "Last-Modified": email.utils.formatdate(time.time(), usegmt=True),
            "x-amz-version-id": self.version_ids.get(key, ""),
        }
        headers.update(self.meta.get(key, {}))
        return headers

    def __call__(self, request: S3HttpRequest) -> S3HttpResponse:
        parsed = urllib.parse.urlsplit(request.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        key = parsed.path.split(f"/{BUCKET}/", 1)[-1]
        self.requests.append((request.method, key))

        if request.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            body = (
                "<ListBucketResult>"
                + "".join(
                    f"<Contents><Key>{name}</Key></Contents>"
                    for name in sorted(self.objects)
                    if name.startswith(prefix)
                )
                + "</ListBucketResult>"
            ).encode()
            return S3HttpResponse(status_code=200, body=body)
        if request.method == "HEAD":
            if key not in self.objects:
                return S3HttpResponse(status_code=404)
            return S3HttpResponse(status_code=200, headers=self._headers_for(key))
        if request.method == "PUT":
            self.objects[key] = request.body
            self._stamp(key)
            self.meta[key] = {
                k.lower(): v
                for k, v in request.headers.items()
                if k.lower().startswith("x-amz-meta-")
            }
            return S3HttpResponse(status_code=200, headers=self._headers_for(key))
        if request.method == "GET":
            if key not in self.objects:
                return S3HttpResponse(status_code=404)
            return S3HttpResponse(
                status_code=200, headers=self._headers_for(key), body=self.objects[key]
            )
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)


def _driver(transport: Any) -> S3CompatibleSigV4Driver:
    return S3CompatibleSigV4Driver(
        S3SigV4Config(
            endpoint_url="https://fence.example.com",
            bucket=BUCKET,
            region="us-east-1",
            access_key_id="AKID",
            secret_access_key="SECRET",
            prefix=PREFIX,
        ),
        http_transport=transport,
    )


def test_unleased_chunk_is_deleted_and_the_fence_retired() -> None:
    """Baseline: no lease, no race — the chunk goes and nothing is left behind."""
    transport = _LeaseRaceTransport()
    s3 = _driver(transport)
    s3.put_chunk(CHUNK, b"precious-bytes")

    assert s3.delete_chunk_if_unleased(CHUNK) is True
    assert s3.has_chunk(CHUNK) is False
    assert FENCE_KEY not in transport.objects


def test_lease_landing_before_the_final_fence_decision_keeps_the_chunk() -> None:
    """The audited race: a lease arrives after the post-delete re-read.

    Without the final check the driver would retire the fence and report ``True``
    with the chunk already destroyed, so the pusher that just took the lease would
    write against a hole. Fail-closed means: ``False``, bytes back, fence removed
    only once the data is durable again, and the lease object untouched.
    """
    transport = _LeaseRaceTransport()
    s3 = _driver(transport)
    s3.put_chunk(CHUNK, b"precious-bytes")

    lease_expiry = 4_102_444_800.0  # far future: 2100-01-01T00:00:00Z
    s3._fence_race_probe = lambda: transport.put_lease(lease_expiry)

    assert s3.delete_chunk_if_unleased(CHUNK) is False
    assert s3.has_chunk(CHUNK) is True
    assert s3.get_chunk(CHUNK) == b"precious-bytes"
    assert FENCE_KEY not in transport.objects
    assert LEASE_KEY in transport.objects


def test_final_check_runs_after_the_post_delete_verification() -> None:
    """Ordering proof: the seam fires after the chunk DELETE, not before it.

    The probe can only be reached once the two-sided fence has already deleted and
    re-verified the chunk; asserting the object is gone at that instant pins where
    in the protocol the last listing happens (a probe firing earlier would leave
    the chunk in place).
    """
    transport = _LeaseRaceTransport()
    s3 = _driver(transport)
    s3.put_chunk(CHUNK, b"precious-bytes")

    seen: dict[str, bool] = {}
    s3._fence_race_probe = lambda: seen.update(chunk_present=CHUNK_KEY in transport.objects)

    assert s3.delete_chunk_if_unleased(CHUNK) is True
    assert seen == {"chunk_present": False}


def test_expired_lease_is_cleaned_then_never_blocks_the_final_decision() -> None:
    """A stale lease object is not a live claim, and the final check agrees.

    The first pass is refused *by design*: ``read_active_leased_chunks`` deletes the
    expired lease object while checking it, which changes the lease state token the
    fence compares against — so this call fails closed rather than acting on a state
    it has just mutated. The second pass sees no lease at all and is decided by the
    final listing, i.e. a dead lease never permanently pins a chunk.
    """
    transport = _LeaseRaceTransport()
    s3 = _driver(transport)
    s3.put_chunk(CHUNK, b"precious-bytes")
    transport.put_lease(1.0)  # 1970: long expired

    assert s3.delete_chunk_if_unleased(CHUNK) is False
    assert s3.has_chunk(CHUNK) is True
    assert LEASE_KEY not in transport.objects  # lazily reaped, not honoured

    assert s3.delete_chunk_if_unleased(CHUNK) is True
    assert s3.has_chunk(CHUNK) is False
    assert FENCE_KEY not in transport.objects


class _VersionedBucketTransport:
    """In-memory S3 REST surface with ETags and *real* conditional-write semantics.

    The two-driver race needs object versions to matter: the fence protocol is built
    on ``If-None-Match: *`` (claim a chunk), ``If-Match: <etag>`` (take over a provably
    expired claim, retire one's own, roll a lease refresh back) and ``If-None-Match: *``
    on the chunk restore. A transport that ignored those headers would make the
    protocol look correct no matter what the driver did, so every conditional write
    here is evaluated against the stored version and answered with 412 on mismatch,
    exactly as S3 does.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        #: ``x-amz-version-id`` per key: the bucket's own object version. It advances
        #: on *every* write, including a rewrite of identical bytes — which is exactly
        #: the ABA replacement a content-addressed ETag cannot see on its own.
        self.version_ids: dict[str, str] = {}
        self.meta: dict[str, dict[str, str]] = {}
        self._writes = 0
        #: The bucket's own clock per object. Real S3 always reports
        #: ``Last-Modified``, and it is what makes a lease's lifetime independent of
        #: the writer's (possibly wrong) clock.
        self.modified: dict[str, float] = {}
        self.versions: dict[str, int] = {}
        self.requests: list[tuple[str, str]] = []

    def _etag(self, key: str, body: bytes) -> str:
        """The ETag S3 actually computes: a hash of the bytes, nothing else.

        Deliberately *not* version-bearing. Real buckets reproduce the same ETag when
        identical bytes are rewritten, and that is the model the driver has to survive:
        the version id and the generation stamp are what make the rewrite visible.
        """
        del key
        return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'

    def write(
        self,
        key: str,
        body: bytes,
        *,
        modified_at: float | None = None,
        meta: dict[str, str] | None = None,
    ) -> None:
        """Out-of-band (unconditional) write, for seeding the bucket in a test."""
        self._writes += 1
        self.objects[key] = body
        self.etags[key] = self._etag(key, body)
        self.version_ids[key] = f"v{self._writes}"
        if meta is not None:
            self.meta[key] = dict(meta)
        self.modified[key] = time.time() if modified_at is None else modified_at

    def _meta_headers(self, key: str) -> dict[str, str]:
        headers = {
            "ETag": self.etags[key],
            "Last-Modified": email.utils.formatdate(self.modified[key], usegmt=True),
            "x-amz-version-id": self.version_ids.get(key, ""),
        }
        headers.update(self.meta.get(key, {}))
        return headers

    def __call__(self, request: S3HttpRequest) -> S3HttpResponse:
        parsed = urllib.parse.urlsplit(request.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        key = parsed.path.split(f"/{BUCKET}/", 1)[-1]
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.requests.append((request.method, key))

        if request.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            body = (
                "<ListBucketResult>"
                + "".join(
                    f"<Contents><Key>{name}</Key></Contents>"
                    for name in sorted(self.objects)
                    if name.startswith(prefix)
                )
                + "</ListBucketResult>"
            ).encode()
            return S3HttpResponse(status_code=200, body=body)
        if request.method == "HEAD":
            if key not in self.objects:
                return S3HttpResponse(status_code=404)
            return S3HttpResponse(status_code=200, headers=self._meta_headers(key))
        if request.method == "GET":
            if key not in self.objects:
                return S3HttpResponse(status_code=404)
            return S3HttpResponse(
                status_code=200, headers=self._meta_headers(key), body=self.objects[key]
            )
        if request.method == "PUT":
            if headers.get("if-none-match") == "*" and key in self.objects:
                return S3HttpResponse(status_code=412)
            required = headers.get("if-match")
            if required is not None and (key not in self.objects or self.etags[key] != required):
                return S3HttpResponse(status_code=412)
            self.write(
                key,
                request.body,
                meta={k: v for k, v in headers.items() if k.startswith("x-amz-meta-")},
            )
            return S3HttpResponse(status_code=200, headers=self._meta_headers(key))
        if request.method == "DELETE":
            required = headers.get("if-match")
            if "gc-fences" in key and required is None:
                # Structural guard for the whole file: a fence is never deleted without
                # a precondition. If the driver ever downgrades to an unconditional
                # DELETE, every test using this transport fails here instead of passing
                # on a permissive fake.
                return S3HttpResponse(status_code=428)
            if required is not None and (key not in self.objects or self.etags[key] != required):
                return S3HttpResponse(status_code=412)
            self.objects.pop(key, None)
            self.etags.pop(key, None)
            self.modified.pop(key, None)
            self.version_ids.pop(key, None)
            self.meta.pop(key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)


def _covering_leases(transport: _VersionedBucketTransport) -> list[str]:
    """Lease objects that are still valid *and* name :data:`CHUNK`."""
    now = time.time()
    covering: list[str] = []
    for key, body in transport.objects.items():
        if not (key.startswith(f"{PREFIX}/leases/") and key.endswith(".json")):
            continue
        try:
            lease = json.loads(body)
        except ValueError:  # a truncated lease is not a claim
            continue
        if CHUNK in lease.get("chunk_ids", ()) and float(lease.get("expires_at_epoch", 0.0)) > now:
            covering.append(key)
    return covering


def test_two_independent_drivers_never_leave_a_valid_lease_over_a_deleted_chunk() -> None:
    """The audited hole, driven by the real writer path from the collector's last window.

    Two *fully independent* driver objects share one bucket transport and nothing
    else: separate ``_coord_lock``, separate clocks, separate state. That is the only
    configuration in which the race is real — a shared in-process lock would serialise
    the two sides and prove nothing about the object protocol.

    The writer is driven from ``_post_final_check_race_probe``, i.e. the instant the
    audit identified: the collector's final lease listing has just succeeded and its
    fence delete is one round trip away. The seam fires *inside* the collector, so the
    interleaving is exact rather than probabilistic, and the assertion is the safety
    property itself, not a particular branch of it:

        **no valid lease may cover a chunk that is absent from the bucket.**

    Either the writer is refused (its lease was never created, or was deleted again
    after the post-write fence read) and the collector may report ``True``, or the
    writer's lease survived and the collector must have restored the bytes and
    reported ``False``. Both are acceptable; the combination "chunk gone, lease
    alive" is not.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    assert gc_driver is not writer_driver
    assert gc_driver._coord_lock is not writer_driver._coord_lock
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    refused: list[str] = []
    leased: list[str] = []

    def _writer_races_in() -> None:
        try:
            leased.append(
                writer_driver.acquire_push_lease([CHUNK], project_id="project-1", ttl_seconds=600.0)
            )
        except VaultRevisionConflictError as exc:
            refused.append(str(exc))

    gc_driver._post_final_check_race_probe = _writer_races_in
    collected = gc_driver.delete_chunk_if_unleased(CHUNK)

    chunk_present = gc_driver.has_chunk(CHUNK)
    covering = _covering_leases(transport)
    # The invariant under audit, stated once and checked unconditionally.
    assert not covering or chunk_present, (
        "a valid lease covers a chunk that the collector removed: "
        f"leases={covering} present={chunk_present} collected={collected}"
    )
    # ... and the collector's own report must agree with the bytes.
    assert collected is (not chunk_present)
    # This interleaving really did reach the writer, and it went down exactly one of
    # the two documented paths — never a third one where both sides believe they won.
    assert bool(refused) != bool(leased)
    if refused:
        assert "GC fence is live" in refused[0]
        assert covering == []
    else:
        assert collected is False
        assert gc_driver.get_chunk(CHUNK) == b"precious-bytes"
        assert covering == [f"{PREFIX}/leases/{leased[0]}.json"]
    # Whatever happened, the collector left no claim of its own behind.
    assert FENCE_KEY not in transport.objects


def test_writer_refuses_a_chunk_a_collector_is_currently_fenced_for() -> None:
    """The writer's half: a live fence is a refusal, not something to delete.

    The pre-fix writer deleted every fence in the leased set, which meant the party
    the fence protects against could remove it. Here the fence is planted by the
    collector driver and the writer must fail closed: no lease object may survive the
    refusal, and the collector's claim must be untouched.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    claim = gc_driver._create_gc_fence(CHUNK, now_epoch=time.time())
    assert claim is not None
    fence_body = transport.objects[FENCE_KEY]
    assert (
        fence_body
        == json.dumps(json.loads(fence_body), sort_keys=True, separators=(",", ":")).encode()
        or b"fence_token" in fence_body
    )

    with pytest.raises(VaultRevisionConflictError, match="GC fence is live"):
        writer_driver.acquire_push_lease([CHUNK], project_id="project-1")
    assert [k for k in transport.objects if k.startswith(f"{PREFIX}/leases/")] == []
    assert transport.objects[FENCE_KEY] == fence_body
    # A second collector cannot take a live claim either — one owner per chunk.
    assert _driver(transport)._create_gc_fence(CHUNK, now_epoch=time.time()) is None
    # And the chunk the fence guards is still there, untouched by anyone.
    assert gc_driver.has_chunk(CHUNK) is True


def test_collector_restores_the_chunk_when_a_clock_skewed_writer_wins_the_last_window() -> None:
    """The other branch of the same protocol: the newcomer's lease really does land.

    Clocks are not shared between processes. A writer whose clock runs ahead of the
    collector's sees the collector's fence as *already expired* (dead debris, which is
    explicitly not a refusal), so it registers its lease inside the exact window the
    audit named — after the collector's final listing, one round trip before the fence
    delete. That is why "the fence was mine a moment ago" cannot be the last word: the
    collector re-reads the lease set after the probe, sees the newcomer, and restores
    the bytes. The lease is then both valid and honest, and the chunk is present —
    the invariant holds from the other side.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    assert gc_driver._coord_lock is not writer_driver._coord_lock
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    skew = datetime.now(timezone.utc) + timedelta(hours=2)
    writer_driver._clock = lambda: skew

    leased: list[str] = []

    def _writer_races_in() -> None:
        leased.append(writer_driver.acquire_push_lease([CHUNK], project_id="project-1"))

    gc_driver._post_final_check_race_probe = _writer_races_in
    collected = gc_driver.delete_chunk_if_unleased(CHUNK)

    # The writer got in, so the collector must stand down and put the bytes back.
    assert leased, "the skewed writer was expected to register its lease"
    assert collected is False
    assert gc_driver.has_chunk(CHUNK) is True
    assert gc_driver.get_chunk(CHUNK) == b"precious-bytes"
    assert _covering_leases(transport) == [f"{PREFIX}/leases/{leased[0]}.json"]
    # The rollback is a create-if-absent write, so it cannot have overwritten a newer
    # object with the staged copy; and the collector's claim is gone either way.
    assert FENCE_KEY not in transport.objects


def test_lease_registered_while_the_fence_is_being_retired_is_still_seen() -> None:
    """The post-retirement listing is load-bearing, and this is the only way to prove it.

    The last pre-retirement check happens one round trip before the fence delete, so a
    lease can still land in that gap. There is no seam there by design — the gap is
    closed by *reading the lease set again after the fence is gone*, not by another
    check before it. Wrapping ``_retire_gc_fence`` puts a clock-skewed writer's lease
    exactly in that gap: the collector's checks all succeed, the fence is retired, and
    only the post-retirement read can still see the newcomer.

    A clock-skewed writer is used because a writer with a shared clock is correctly
    refused while the fence is live — the interesting case is the one where the fence
    looks dead to the writer but is still owned by a running collector.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    skew = datetime.now(timezone.utc) + timedelta(hours=2)
    writer_driver._clock = lambda: skew

    leased: list[str] = []
    original_retire = gc_driver._retire_gc_fence

    def _retire_after_the_latecomer_arrives(chunk_id: str, token: str) -> bool:
        leased.append(writer_driver.acquire_push_lease([CHUNK], project_id="project-1"))
        return original_retire(chunk_id, token)

    gc_driver._retire_gc_fence = _retire_after_the_latecomer_arrives  # type: ignore[method-assign]

    assert gc_driver.delete_chunk_if_unleased(CHUNK) is False
    assert leased, "the writer was expected to register its lease inside the gap"
    assert gc_driver.has_chunk(CHUNK) is True
    assert gc_driver.get_chunk(CHUNK) == b"precious-bytes"
    assert _covering_leases(transport) == [f"{PREFIX}/leases/{leased[0]}.json"]


def test_conditional_restore_never_overwrites_a_newer_chunk_object() -> None:
    """Rollback is a create-if-absent write: a newer object always wins.

    The collector stages the bytes it is about to delete, so by the time it decides to
    roll back, the world may have moved on — a writer can have published a newer
    envelope for the same chunk. An unconditional ``PUT`` here would silently revert
    that newer object to the stale staged copy. ``If-None-Match: *`` turns the rollback
    into a no-op instead.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    assert gc_driver._coord_lock is not writer_driver._coord_lock
    gc_driver.put_chunk(CHUNK, b"staged-bytes")

    skew = datetime.now(timezone.utc) + timedelta(hours=2)
    writer_driver._clock = lambda: skew
    leased: list[str] = []

    def _writer_publishes_a_newer_object() -> None:
        leased.append(writer_driver.acquire_push_lease([CHUNK], project_id="project-1"))
        transport.write(CHUNK_KEY, b"newer-envelope-bytes")

    gc_driver._post_final_check_race_probe = _writer_publishes_a_newer_object
    assert gc_driver.delete_chunk_if_unleased(CHUNK) is False
    assert leased
    # The newer object survived the rollback untouched — no revert to staged bytes.
    assert transport.objects[CHUNK_KEY] == b"newer-envelope-bytes"


def test_restore_refuses_bytes_whose_envelope_names_another_chunk() -> None:
    """Identity before durability: staged bytes must name the key they are replacing."""
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    foreign_header = b"NPCV" + bytes([2, 0]) + bytes.fromhex("d" * 64)
    foreign_envelope = len(foreign_header).to_bytes(2, "big") + foreign_header + b"\x00" * 32

    with pytest.raises(VaultTamperDetectedError):
        gc_driver._restore_chunk_conditionally(CHUNK, foreign_envelope)
    assert CHUNK_KEY not in transport.objects


def test_fence_retirement_refuses_a_claim_that_now_belongs_to_another_collector() -> None:
    """A collector retires only its own claim — never a newer collector's.

    The token is re-read inside :meth:`_retire_gc_fence` rather than trusted from the
    caller, so a fence that was replaced (its owner crashed and a second collector took
    the chunk over) is left alone. Erasing it would hand the chunk to a third collector
    while the second is still working on it.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    claim = gc_driver._create_gc_fence(CHUNK, now_epoch=time.time())
    assert claim is not None
    our_token, _ = claim
    successor_body = json.dumps(
        {
            "fence_token": "successor-token",
            "chunk_id": CHUNK,
            "created_at_epoch": time.time(),
            "expires_at_epoch": time.time() + 300.0,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    transport.write(FENCE_KEY, successor_body)

    assert gc_driver._retire_gc_fence(CHUNK, our_token) is False
    assert transport.objects[FENCE_KEY] == successor_body


class _NoEtagTransport(_VersionedBucketTransport):
    """A backend that answers fence reads without an ``ETag`` (no conditional delete).

    Real S3 always reports one, but "the backend told us nothing" must never be read
    as "unconditional delete is fine": that is exactly the downgrade that lets a
    retiring collector destroy a newer owner's claim.
    """

    def __call__(self, request: S3HttpRequest) -> S3HttpResponse:
        response = super().__call__(request)
        if "gc-fences" not in request.url:
            return response
        stripped = {k: v for k, v in response.headers.items() if k.lower() != "etag"}
        return S3HttpResponse(
            status_code=response.status_code, headers=stripped, body=response.body
        )


def test_aba_collector_finishing_before_the_lease_write_forces_a_rollback() -> None:
    """The ABA race: a full collector cycle completes between preflight and PUT.

    The writer checks for a live fence, then is stopped. The collector claims the
    chunk, deletes it, retires its fence and returns ``True``. The writer wakes up,
    writes its lease, looks for a live fence again and finds none — because the
    collector already cleaned up after itself. A fence-only protocol publishes a
    lease over a chunk that no longer exists.

    The generation proof closes it: the chunk existed when the lease was preflighted
    (so its ETag was recorded), it is gone now, and the lease is deleted again before
    the error leaves this method. Nothing valid is left on the bucket.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    assert gc_driver._coord_lock is not writer_driver._coord_lock
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    observed: dict[str, object] = {}

    def _collector_runs_to_completion() -> None:
        observed["collected"] = gc_driver.delete_chunk_if_unleased(CHUNK)
        observed["present_after_gc"] = gc_driver.has_chunk(CHUNK)
        observed["fence_left"] = FENCE_KEY in transport.objects

    writer_driver._pre_lease_put_race_probe = _collector_runs_to_completion

    with pytest.raises(VaultRevisionConflictError, match="disappeared or changed generation"):
        writer_driver.acquire_push_lease([CHUNK], project_id="project-1", ttl_seconds=600.0)

    # The interleaving really was the ABA one: GC finished, returned True, and left
    # no fence behind for a second-look check to notice.
    assert observed == {"collected": True, "present_after_gc": False, "fence_left": False}
    # Fail-closed: no lease object survived, and the chunk is genuinely gone.
    assert [k for k in transport.objects if k.startswith(f"{PREFIX}/leases/")] == []
    assert _covering_leases(transport) == []
    assert gc_driver.has_chunk(CHUNK) is False


def test_aba_refreshing_a_lease_after_the_chunk_was_collected_rolls_back() -> None:
    """Same ABA race on the refresh path, which is where chunks are actually added.

    ``_acquire_driver_lease`` takes an *empty* lease and every chunk is added by a
    refresh before it is uploaded, so a refresh that promises a chunk the collector
    has just deleted is the production shape of this bug.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")
    lease_id = writer_driver.acquire_push_lease([], project_id="project-1")

    def _collector_runs_to_completion() -> None:
        assert gc_driver.delete_chunk_if_unleased(CHUNK) is True

    writer_driver._pre_lease_put_race_probe = _collector_runs_to_completion
    with pytest.raises(VaultRevisionConflictError, match="disappeared or changed generation"):
        writer_driver.update_push_lease(lease_id, [CHUNK], project_id="project-1")

    assert _covering_leases(transport) == []
    assert gc_driver.has_chunk(CHUNK) is False


def test_a_lagging_writer_clock_never_makes_its_fresh_lease_look_expired() -> None:
    """Negative skew: the writer's clock is two hours behind the collector's.

    ``expires_at_epoch`` is written by the writer, so it lands two hours in the
    collector's past and a naive reader reaps a lease that is a second old — then
    deletes the chunk it protects. The bucket's own ``Last-Modified`` plus the lease's
    ``ttl_seconds`` re-derive the same lifetime on a clock neither side controls.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    lagging = datetime.now(timezone.utc) - timedelta(hours=2)
    writer_driver._clock = lambda: lagging
    lease_id = writer_driver.acquire_push_lease([CHUNK], project_id="project-1", ttl_seconds=600.0)

    written = json.loads(transport.objects[f"{PREFIX}/leases/{lease_id}.json"])
    # The lease itself claims it expired almost two hours ago...
    assert written["expires_at_epoch"] < time.time() - 3600.0
    # ...and the collector must still honour it.
    assert gc_driver.read_active_leased_chunks() == {CHUNK}
    assert gc_driver.delete_chunk_if_unleased(CHUNK) is False
    assert gc_driver.has_chunk(CHUNK) is True
    assert f"{PREFIX}/leases/{lease_id}.json" in transport.objects


def test_a_leading_writer_clock_never_shortens_the_lease_it_wrote() -> None:
    """Positive skew: an hour ahead is still an active lease, never a stale one."""
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    leading = datetime.now(timezone.utc) + timedelta(hours=1)
    writer_driver._clock = lambda: leading
    writer_driver.acquire_push_lease([CHUNK], project_id="project-1", ttl_seconds=600.0)

    assert gc_driver.read_active_leased_chunks() == {CHUNK}
    assert gc_driver.delete_chunk_if_unleased(CHUNK) is False
    assert gc_driver.has_chunk(CHUNK) is True


def test_fence_retirement_never_degrades_to_an_unconditional_delete() -> None:
    """No ETag means no conditional DELETE, and no DELETE means no data loss.

    The pre-fix code fell back to ``DELETE`` without headers when the backend reported
    no ETag, which can erase a *newer* owner's claim that landed one round trip
    earlier. Refusing costs a retry until the TTL; deleting the wrong fence costs a
    chunk.
    """
    transport = _NoEtagTransport()
    gc_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    claim = gc_driver._create_gc_fence(CHUNK, now_epoch=time.time())
    assert claim is not None
    token, _ = claim
    observed = gc_driver._read_gc_fence(CHUNK)
    assert observed is not None and observed.etag is None

    assert gc_driver._retire_gc_fence(CHUNK, token) is False
    assert gc_driver._delete_gc_fence_if_unchanged(CHUNK, observed) is False
    assert transport.objects[FENCE_KEY] == observed.raw
    # End to end the collector cannot retire its claim, so it must not publish the
    # deletion either: fail-closed, chunk intact.
    assert gc_driver.delete_chunk_if_unleased(CHUNK) is False
    assert gc_driver.get_chunk(CHUNK) == b"precious-bytes"


def test_a_fence_replaced_between_read_and_delete_is_never_removed() -> None:
    """Both halves of the guard: changed bytes, and unchanged bytes with a new ETag."""
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")
    assert gc_driver._create_gc_fence(CHUNK, now_epoch=time.time()) is not None
    observed = gc_driver._read_gc_fence(CHUNK)
    assert observed is not None

    successor = json.dumps(
        {
            "fence_token": "successor-token",
            "chunk_id": CHUNK,
            "created_at_epoch": time.time(),
            "expires_at_epoch": time.time() + 300.0,
            "ttl_seconds": 300.0,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    transport.write(FENCE_KEY, successor)
    assert gc_driver._delete_gc_fence_if_unchanged(CHUNK, observed) is False
    assert transport.objects[FENCE_KEY] == successor

    # Identical bytes rewritten by another collector: a content check cannot see it,
    # so this is exactly what the conditional DELETE has to cover. The transport
    # refuses any unconditional fence delete outright (HTTP 428), so a downgrade can
    # never pass silently; this delete is re-issued against the fresh ETag and
    # therefore succeeds legitimately.
    transport.write(FENCE_KEY, observed.raw)
    assert gc_driver._delete_gc_fence_if_unchanged(CHUNK, observed) is True
    assert FENCE_KEY not in transport.objects

    # And the owner can still retire its own claim when nothing moved under it.
    assert gc_driver._create_gc_fence(CHUNK, now_epoch=time.time()) is not None
    current = gc_driver._read_gc_fence(CHUNK)
    assert current is not None
    assert gc_driver._delete_gc_fence_if_unchanged(CHUNK, current) is True
    assert FENCE_KEY not in transport.objects


class _NoLeaseEtagTransport(_VersionedBucketTransport):
    """A backend that reports no ``ETag`` for *lease* objects.

    Chunks are answered normally — this models the one shape that matters for the
    reaper: a lease whose identity the backend refuses to state.
    """

    def __call__(self, request: S3HttpRequest) -> S3HttpResponse:
        response = super().__call__(request)
        if "leases/" not in request.url:
            return response
        stripped = {k: v for k, v in response.headers.items() if k.lower() != "etag"}
        return S3HttpResponse(
            status_code=response.status_code, headers=stripped, body=response.body
        )


class _NoIdentityTransport(_VersionedBucketTransport):
    """A backend that reports neither a version id nor our generation stamp.

    The ETag survives (buckets do report one), which is exactly the trap: an ETag is
    a hash of the *bytes*, so trusting it alone makes a delete-and-reupload look
    harmless. With no version and no generation the identity is unknowable.
    """

    def __call__(self, request: S3HttpRequest) -> S3HttpResponse:
        response = super().__call__(request)
        if "chunks/" not in request.url:
            return response
        stripped = {
            k: v
            for k, v in response.headers.items()
            if k.lower() != "x-amz-version-id" and not k.lower().startswith("x-amz-meta-")
        }
        return S3HttpResponse(
            status_code=response.status_code, headers=stripped, body=response.body
        )


def test_stale_lease_cleanup_cannot_delete_a_concurrently_refreshed_lease() -> None:
    """A refresh that lands between the listing read and the reap keeps its chunk.

    ``read_active_leased_chunks`` used to reap an expired lease with an
    unconditional ``DELETE``. The lease it deleted was the one it had read one round
    trip earlier — and a writer that refreshes its lease in that gap is *by
    definition* re-extending the deadline, so the delete destroyed a live claim and
    the chunk it protected was deleted in the same pass.
    """
    transport = _VersionedBucketTransport()
    gc_driver = _driver(transport)
    writer_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    lease_id = writer_driver.acquire_push_lease(
        [CHUNK], project_id="project-1", ttl_seconds=60.0, lease_id="w-1"
    )
    lease_key = f"{PREFIX}/leases/{lease_id}.json"
    refreshed = json.dumps(
        {
            "lease_id": lease_id,
            "pid": 4242,
            "project_id": "project-1",
            "lease_token": "refreshed-token",
            "created_at_epoch": time.time(),
            "expires_at_epoch": time.time() + 3600.0,
            "ttl_seconds": 3600.0,
            "chunk_ids": [CHUNK],
        }
    ).encode()

    state = {"refreshed": False}

    def _refresh_lands_mid_pass(request: S3HttpRequest) -> S3HttpResponse:
        response = transport(request)
        if (
            not state["refreshed"]
            and request.method == "GET"
            and request.url.endswith(f"/{lease_id}.json")
        ):
            # The writer's refresh commits after our listing read returned.
            state["refreshed"] = True
            transport.write(lease_key, refreshed)
        return response

    driver = _driver(_refresh_lands_mid_pass)
    far_future = time.time() + 7200.0

    assert driver.read_active_leased_chunks(now_epoch=far_future) == {CHUNK}
    assert lease_key in transport.objects
    assert transport.objects[lease_key] == refreshed
    assert gc_driver.delete_chunk_if_unleased(CHUNK) is False
    assert gc_driver.has_chunk(CHUNK) is True


def test_failed_rollback_never_deletes_a_third_writers_newer_lease() -> None:
    """A refused refresh rolls back its *own* write, never a successor's lease.

    The rollback used to run ``self._send("DELETE", object_key=obj_key)`` whenever it
    could not restore the prior body. Here the refresh is refused by a live fence
    (so it *must* roll back) while a third writer has already replaced the lease
    object; the rollback must observe the replacement and leave it alone.
    """
    transport = _VersionedBucketTransport()
    writer = _driver(transport)
    writer.put_chunk(CHUNK, b"precious-bytes")
    lease_id = writer.acquire_push_lease(
        [CHUNK], project_id="project-1", ttl_seconds=600.0, lease_id="shared-1"
    )
    lease_key = f"{PREFIX}/leases/{lease_id}.json"
    credential = writer.export_lease_credential(lease_id)

    third_writer = json.dumps(
        {
            "lease_id": lease_id,
            "pid": 999,
            "project_id": "project-9",
            "lease_token": "third-writer-token",
            "created_at_epoch": time.time(),
            "expires_at_epoch": time.time() + 900.0,
            "ttl_seconds": 900.0,
            "chunk_ids": [CHUNK],
        }
    ).encode()

    state = {"replaced": False}

    def _third_writer_lands_after_our_put(request: S3HttpRequest) -> S3HttpResponse:
        response = transport(request)
        if (
            not state["replaced"]
            and request.method == "PUT"
            and request.url.endswith(f"/{lease_id}.json")
        ):
            state["replaced"] = True
            transport.write(lease_key, third_writer)
        return response

    racer = _driver(_third_writer_lands_after_our_put)
    racer.recover_owned_lease("project-1", lease_id, credential["lease_token"], credential["etag"])

    def _collector_claims_the_chunk() -> None:
        # A live fence makes the post-write check refuse, which is what triggers the
        # rollback this test is about.
        assert racer._create_gc_fence(CHUNK, now_epoch=time.time()) is not None

    racer._pre_lease_put_race_probe = _collector_claims_the_chunk

    with pytest.raises(VaultRevisionConflictError):
        racer.update_push_lease(
            lease_id,
            [CHUNK],
            project_id="project-1",
            ttl_seconds=600.0,
            expected_etag=credential["etag"],
        )

    assert state["replaced"] is True
    assert lease_key in transport.objects
    assert transport.objects[lease_key] == third_writer
    assert racer.has_chunk(CHUNK) is True


def test_absent_lease_etag_never_causes_unconditional_lease_deletion() -> None:
    """No ETag means no delete: the lease stays and its chunk stays protected.

    A lease object whose identity the backend does not state cannot be proven to be
    the one that was just read, so it cannot be reaped. Fail-closed here costs one
    GC pass; the alternative is deleting a claim that a writer refreshed (or a
    different writer created) one round trip ago.
    """
    transport = _NoLeaseEtagTransport()
    driver = _driver(transport)
    gc_driver = _driver(transport)
    gc_driver.put_chunk(CHUNK, b"precious-bytes")

    lease_id = "stale-1"
    lease_key = f"{PREFIX}/leases/{lease_id}.json"
    transport.write(
        lease_key,
        json.dumps(
            {
                "lease_id": lease_id,
                "pid": 7,
                "project_id": "project-1",
                "lease_token": "stale-token",
                "created_at_epoch": 1.0,
                "expires_at_epoch": 2.0,
                "ttl_seconds": 1.0,
                "chunk_ids": [CHUNK],
            }
        ).encode(),
        modified_at=1.0,
    )

    # Long expired, but with no ETag to condition the delete on: not reaped.
    assert driver.read_active_leased_chunks(now_epoch=time.time() + 10_000.0) == {CHUNK}
    assert lease_key in transport.objects

    # And asking to release it with an empty ETag is refused outright — the empty
    # string is "unknown identity", not a licence for an unconditional DELETE.
    with pytest.raises(VaultTransportError):
        driver.release_push_lease(lease_id, expected_etag="")
    assert lease_key in transport.objects
    assert gc_driver.has_chunk(CHUNK) is True


def test_identical_bytes_reupload_with_same_etag_is_detected_as_aba_replacement() -> None:
    """Delete-and-reupload of identical bytes is a *new* object, not the same one.

    Chunk ids are content-addressed, so the ETag is a function of the bytes alone.
    The collector below removes the chunk and a writer republishes the same bytes:
    same length, same ETag, same content — and a different object. If identity were
    the ETag, the lease would be published over a chunk the collector had already
    deleted; the generation stamp and the version id are what make it visible.
    """
    transport = _VersionedBucketTransport()
    writer = _driver(transport)
    writer.put_chunk(CHUNK, b"precious-bytes")
    etag_before = transport.etags[CHUNK_KEY]

    def _aba_replacement() -> None:
        transport.objects.pop(CHUNK_KEY, None)
        transport.write(CHUNK_KEY, b"precious-bytes")

    # The proof is only meaningful if the ETag really is unchanged by the rewrite.
    assert transport.etags[CHUNK_KEY] == etag_before
    writer._pre_lease_put_race_probe = _aba_replacement

    with pytest.raises(VaultRevisionConflictError, match="disappeared or changed generation"):
        writer.acquire_push_lease([CHUNK], project_id="project-1", ttl_seconds=600.0)

    assert transport.etags[CHUNK_KEY] == etag_before
    assert [k for k in transport.objects if "/leases/" in k] == []


def test_missing_version_and_generation_metadata_fails_closed() -> None:
    """An object the bucket cannot identify is a conflict, not "unchanged".

    The pre-fix identity was the ETag alone. A backend that reports an ETag but no
    version id and no generation stamp gives a value that cannot distinguish two
    different objects with the same bytes, so "equal" there is not a proof of
    anything. Refusing is the only answer that does not guess.
    """
    transport = _NoIdentityTransport()
    writer = _driver(transport)
    writer.put_chunk(CHUNK, b"precious-bytes")

    assert writer._chunk_generation(CHUNK) == "UNKNOWN_GENERATION"

    with pytest.raises(VaultRevisionConflictError, match="disappeared or changed generation"):
        writer.acquire_push_lease([CHUNK], project_id="project-1", ttl_seconds=600.0)

    assert [k for k in transport.objects if "/leases/" in k] == []
    assert writer.has_chunk(CHUNK) is True


class _LeaseEtagFaultTransport(_VersionedBucketTransport):
    """A bucket whose *lease reads* misbehave in the three ways that used to break.

    ``strip_etag_on`` omits the ``ETag`` on lease ``HEAD`` and/or ``GET`` (a backend that
    answers without one), ``get_status`` fails the lease ``GET`` outright, and
    ``get_etag_override`` makes the ``GET`` disagree with the ``HEAD`` — a lease
    replaced between the two reads. The write path is never tampered with: the point is
    what the *refresh* does with a prior state it cannot trust.
    """

    def __init__(
        self,
        *,
        strip_etag_on: str = "",
        get_status: int | None = None,
        get_etag_override: str | None = None,
    ) -> None:
        super().__init__()
        self.strip_etag_on = strip_etag_on
        self.get_status = get_status
        self.get_etag_override = get_etag_override

    def __call__(self, request: S3HttpRequest) -> S3HttpResponse:
        response = super().__call__(request)
        if "leases/" not in request.url:
            return response
        if request.method == "GET" and self.get_status is not None:
            return S3HttpResponse(status_code=self.get_status)
        if request.method not in ("HEAD", "GET"):
            return response
        strip = self.strip_etag_on == "both" or self.strip_etag_on == request.method.lower()
        headers = {k: v for k, v in response.headers.items() if not (strip and k.lower() == "etag")}
        if request.method == "GET" and self.get_etag_override is not None:
            headers["etag"] = self.get_etag_override
        return S3HttpResponse(status_code=response.status_code, headers=headers, body=response.body)


class _NoVersionIdTransport(_VersionedBucketTransport):
    """A bucket without object versioning: chunks answer with an ETag and our stamp only."""

    def __call__(self, request: S3HttpRequest) -> S3HttpResponse:
        response = super().__call__(request)
        if "chunks/" not in request.url:
            return response
        stripped = {k: v for k, v in response.headers.items() if k.lower() != "x-amz-version-id"}
        return S3HttpResponse(
            status_code=response.status_code, headers=stripped, body=response.body
        )


@pytest.mark.parametrize("strip", ["head", "get", "both"])
def test_update_push_lease_refuses_to_send_put_when_head_or_get_lacks_etag(strip: str) -> None:
    """No ETag on the prior lease means no refresh at all — not an unconditional PUT.

    The pre-fix code built ``conditional = {}`` when the ``HEAD`` reported no ETag and
    then sent a plain ``PUT``, i.e. it overwrote whatever lease was there and only
    *afterwards* discovered it could no longer prove ownership. Refusing before the
    write costs a retry; overwriting costs the lease (and the chunks it protects).
    """
    transport = _LeaseEtagFaultTransport(strip_etag_on=strip)
    writer = _driver(transport)
    writer.put_chunk(CHUNK, b"precious-bytes")
    lease_id = writer.acquire_push_lease(
        [CHUNK], project_id="project-1", ttl_seconds=600.0, lease_id="etag-less-1"
    )
    lease_key = f"{PREFIX}/leases/{lease_id}.json"
    before = transport.objects[lease_key]
    puts_before = [entry for entry in transport.requests if entry == ("PUT", lease_key)]
    assert puts_before, "the acquire itself must have written the lease"

    with pytest.raises(VaultTransportError, match="non-empty ETag precondition"):
        writer.update_push_lease(lease_id, [CHUNK], project_id="project-1", ttl_seconds=600.0)

    assert [entry for entry in transport.requests if entry == ("PUT", lease_key)] == puts_before
    assert transport.objects[lease_key] == before
    assert [entry for entry in transport.requests if entry[0] == "DELETE"] == []


def test_update_push_lease_aborts_before_put_when_prior_get_fails_or_disagrees_with_head() -> None:
    """A prior body that cannot be read, or no longer matches the HEAD, stops the refresh.

    ``prior_body = prior.body if prior.status_code == 200 else None`` was the second
    half of the same bug: a failed ``GET`` silently produced "there was no prior lease",
    and the rollback then *deleted* the lease instead of restoring it. A ``GET`` whose
    ETag disagrees with the ``HEAD`` is the same state one round trip later.
    """
    # (a) the prior-body GET fails: no PUT, no DELETE, the lease is untouched.
    failing = _LeaseEtagFaultTransport(get_status=503)
    writer = _driver(failing)
    writer.put_chunk(CHUNK, b"precious-bytes")
    lease_id = writer.acquire_push_lease(
        [CHUNK], project_id="project-1", ttl_seconds=600.0, lease_id="get-fails-1"
    )
    lease_key = f"{PREFIX}/leases/{lease_id}.json"
    before = failing.objects[lease_key]
    puts_before = [entry for entry in failing.requests if entry == ("PUT", lease_key)]

    with pytest.raises(VaultTransportError, match="could not read the prior lease"):
        writer.update_push_lease(lease_id, [CHUNK], project_id="project-1", ttl_seconds=600.0)

    assert [entry for entry in failing.requests if entry == ("PUT", lease_key)] == puts_before
    assert failing.objects[lease_key] == before
    assert [entry for entry in failing.requests if entry[0] == "DELETE"] == []

    # (b) HEAD and GET disagree: the object moved under us, so nothing is written.
    disagreeing = _LeaseEtagFaultTransport(get_etag_override='"a-different-write"')
    writer = _driver(disagreeing)
    writer.put_chunk(CHUNK, b"precious-bytes")
    lease_id = writer.acquire_push_lease(
        [CHUNK], project_id="project-1", ttl_seconds=600.0, lease_id="head-get-differ"
    )
    lease_key = f"{PREFIX}/leases/{lease_id}.json"
    before = disagreeing.objects[lease_key]
    puts_before = [entry for entry in disagreeing.requests if entry == ("PUT", lease_key)]

    with pytest.raises(VaultRevisionConflictError, match="changed between HEAD and GET"):
        writer.update_push_lease(lease_id, [CHUNK], project_id="project-1", ttl_seconds=600.0)

    assert [entry for entry in disagreeing.requests if entry == ("PUT", lease_key)] == puts_before
    assert disagreeing.objects[lease_key] == before
    assert [entry for entry in disagreeing.requests if entry[0] == "DELETE"] == []


def test_release_refuses_peer_lease_even_when_peer_has_a_real_etag() -> None:
    """An observed ETag is not authority to release a peer's replacement lease.

    The peer preserves the public lease id and project id but writes a different
    owner verifier. Its transport returns a real, distinct ETag. The original
    owner's recorded ETag and verifier must remain the only release authority.
    """
    transport = _VersionedBucketTransport()
    owner = _driver(transport)
    owner.put_chunk(CHUNK, b"precious-bytes")
    lease_id = owner.acquire_push_lease(
        [CHUNK], project_id="project-1", ttl_seconds=600.0, lease_id="peer-etag-1"
    )
    lease_key = f"{PREFIX}/leases/{lease_id}.json"
    owner_etag = owner.export_lease_credential(lease_id)["etag"]

    peer_body = json.dumps(
        {
            "lease_id": lease_id,
            "pid": 41,
            "project_id": "project-1",
            "lease_token": "peer-secret-token",
            "created_at_epoch": time.time(),
            "expires_at_epoch": time.time() + 900.0,
            "ttl_seconds": 900.0,
            "chunk_ids": [CHUNK],
        }
    ).encode()
    transport.write(lease_key, peer_body)
    peer_etag = transport.etags[lease_key]
    assert peer_etag and peer_etag != owner_etag

    deletes_before = [request for request in transport.requests if request[0] == "DELETE"]
    with pytest.raises(VaultRevisionConflictError):
        owner.release_push_lease(lease_id)
    deletes_after = [request for request in transport.requests if request[0] == "DELETE"]

    assert deletes_after == deletes_before
    assert transport.objects[lease_key] == peer_body
    assert transport.etags[lease_key] == peer_etag


def test_release_without_private_owner_proof_cannot_adopt_observed_etag() -> None:
    """No-argument release is same-instance only; observation is not authority.

    A peer that can read a shared object must not adopt its ETag as release
    authority. A restart can recover, but only with the private token and exact
    ETag exported by the owner; a refreshed version fences out the old owner.
    """
    transport = _VersionedBucketTransport()
    driver_a = _driver(transport)
    driver_b = _driver(transport)
    driver_a.put_chunk(CHUNK, b"precious-bytes")
    lease_id = driver_a.acquire_push_lease(
        [CHUNK], project_id="project-1", ttl_seconds=600.0, lease_id="shared-release-1"
    )
    lease_key = f"{PREFIX}/leases/{lease_id}.json"
    acquired = transport.objects[lease_key]
    credential = driver_a.export_lease_credential(lease_id)

    with pytest.raises(VaultRevisionConflictError):
        driver_b.update_push_lease(
            lease_id,
            [CHUNK],
            project_id="project-1",
            ttl_seconds=900.0,
            expected_etag=credential["etag"],
        )
    with pytest.raises(VaultRevisionConflictError):
        driver_b.release_push_lease(lease_id)
    assert transport.objects[lease_key] == acquired

    assert (
        driver_b.recover_owned_lease(
            "project-1", lease_id, credential["lease_token"], credential["etag"]
        )
        == credential["etag"]
    )
    assert driver_b.update_push_lease(
        lease_id,
        [CHUNK],
        project_id="project-1",
        ttl_seconds=900.0,
        expected_etag=credential["etag"],
    )
    refreshed = transport.objects[lease_key]
    assert refreshed != acquired

    deletes_before = [entry for entry in transport.requests if entry[0] == "DELETE"]
    with pytest.raises(VaultRevisionConflictError):
        driver_a.release_push_lease(lease_id)
    assert transport.objects[lease_key] == refreshed
    deletes_after = [entry for entry in transport.requests if entry[0] == "DELETE"]
    assert len(deletes_after) == len(deletes_before)

    # A stranger cannot promote a read ETag into authority, even if the bucket
    # supplies an ETag. The failed attempt sends no DELETE and preserves the lease.
    stranger = _driver(transport)
    with pytest.raises(VaultRevisionConflictError):
        stranger.release_push_lease(lease_id)
    assert [entry for entry in transport.requests if entry[0] == "DELETE"] == deletes_after
    assert lease_key in transport.objects

    assert driver_b.release_push_lease(lease_id)
    assert lease_key not in transport.objects


def test_identical_bytes_aba_detected_via_metadata_generation_when_bucket_has_no_version_id() -> (
    None
):
    """The generation stamp alone must catch the ABA rewrite on an unversioned bucket.

    Chunk ids are content-addressed, so the ``ETag`` of a delete-and-reupload is
    identical by construction. Without bucket versioning there is no
    ``x-amz-version-id`` to fall back on either: the only evidence left is the
    ``x-amz-meta-nexus-chunk-gen`` header every upload mints, and it has to be enough.
    """
    transport = _NoVersionIdTransport()
    writer = _driver(transport)
    writer.put_chunk(CHUNK, b"precious-bytes")
    etag_before = transport.etags[CHUNK_KEY]
    generation_before = transport.meta[CHUNK_KEY]["x-amz-meta-nexus-chunk-gen"]
    # The driver's own view of the object: no version id (the bucket has none), so the
    # identity it compares is the empty version plus the generation stamp plus the ETag.
    identity_before = writer._chunk_generation(CHUNK)
    assert identity_before is not None
    version_part, generation_part, _etag_part = identity_before.split("|")
    assert version_part == ""
    assert generation_part == generation_before

    def _aba_replacement() -> None:
        transport.objects.pop(CHUNK_KEY, None)
        transport.write(
            CHUNK_KEY,
            b"precious-bytes",
            meta={"x-amz-meta-nexus-chunk-gen": "replacement-generation"},
        )

    writer._pre_lease_put_race_probe = _aba_replacement

    with pytest.raises(VaultRevisionConflictError, match="disappeared or changed generation"):
        writer.acquire_push_lease([CHUNK], project_id="project-1", ttl_seconds=600.0)

    assert transport.etags[CHUNK_KEY] == etag_before
    assert transport.meta[CHUNK_KEY]["x-amz-meta-nexus-chunk-gen"] != generation_before
    assert [key for key in transport.objects if "/leases/" in key] == []
