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

import json
import urllib.parse
from typing import Any

from nexus_ai_agent.storage.personal_cloud_vault import (
    S3CompatibleSigV4Driver,
    S3HttpRequest,
    S3HttpResponse,
    S3SigV4Config,
)

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
