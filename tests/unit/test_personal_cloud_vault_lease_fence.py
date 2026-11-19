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
)
from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import VaultTamperDetectedError

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

    def put_lease(self, expires_at_epoch: float) -> None:
        self.objects[LEASE_KEY] = json.dumps(
            {"expires_at_epoch": expires_at_epoch, "chunk_ids": [CHUNK]}
        ).encode()

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
            return S3HttpResponse(status_code=200 if key in self.objects else 404)
        if request.method == "PUT":
            self.objects[key] = request.body
            return S3HttpResponse(status_code=200)
        if request.method == "GET":
            if key not in self.objects:
                return S3HttpResponse(status_code=404)
            return S3HttpResponse(status_code=200, body=self.objects[key])
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
        self.requests: list[tuple[str, str]] = []

    @staticmethod
    def _etag(body: bytes) -> str:
        return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'

    def write(self, key: str, body: bytes) -> None:
        """Out-of-band (unconditional) write, for seeding the bucket in a test."""
        self.objects[key] = body
        self.etags[key] = self._etag(body)

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
            return S3HttpResponse(status_code=200, headers={"ETag": self.etags[key]})
        if request.method == "GET":
            if key not in self.objects:
                return S3HttpResponse(status_code=404)
            return S3HttpResponse(
                status_code=200, headers={"ETag": self.etags[key]}, body=self.objects[key]
            )
        if request.method == "PUT":
            if headers.get("if-none-match") == "*" and key in self.objects:
                return S3HttpResponse(status_code=412)
            required = headers.get("if-match")
            if required is not None and (key not in self.objects or self.etags[key] != required):
                return S3HttpResponse(status_code=412)
            self.write(key, request.body)
            return S3HttpResponse(status_code=200, headers={"ETag": self.etags[key]})
        if request.method == "DELETE":
            required = headers.get("if-match")
            if required is not None and (key not in self.objects or self.etags[key] != required):
                return S3HttpResponse(status_code=412)
            self.objects.pop(key, None)
            self.etags.pop(key, None)
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
    writer_driver._clock = lambda: skew  # type: ignore[method-assign]

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
    writer_driver._clock = lambda: skew  # type: ignore[method-assign]

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
    writer_driver._clock = lambda: skew  # type: ignore[method-assign]
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
