"""Tests for LocalDeviceVaultDriver, S3CompatibleSigV4Driver, and PortableBundleVaultDriver."""

from __future__ import annotations

import hashlib
import io
import struct
import urllib.error
import urllib.parse
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.storage.personal_cloud_vault import (
    BUNDLE_MAGIC,
    BUNDLE_VERSION,
    BYOSStorageDriver,
    LocalDeviceVaultDriver,
    PersonalVaultError,
    PortableBundleVaultDriver,
    S3CompatibleSigV4Driver,
    S3HttpRequest,
    S3HttpResponse,
    S3SigV4Config,
    VaultAuthenticationError,
    VaultQuotaExceededError,
    VaultTamperDetectedError,
    VaultTransportError,
    compute_sigv4_signature,
)
from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import (
    _BUNDLE_HEADER_PREFIX,
    _BUNDLE_HEADER_TOTAL,
    _BUNDLE_INDEX_HEAD,
    _BUNDLE_INDEX_TAIL,
    _default_urllib_transport,
    _parse_s3_list_xml,
)


def test_all_three_drivers_satisfy_byos_protocol(tmp_path: Path) -> None:
    local = LocalDeviceVaultDriver(tmp_path / "local")
    s3 = S3CompatibleSigV4Driver(
        S3SigV4Config(
            endpoint_url="https://account.r2.cloudflarestorage.com",
            bucket="my-bucket",
            region="auto",
            access_key_id="AKID",
            secret_access_key="SECRET",
        ),
        http_transport=lambda _req: S3HttpResponse(status_code=200),
    )
    bundle = PortableBundleVaultDriver(tmp_path / "vault.nagarvault")

    assert isinstance(local, BYOSStorageDriver)
    assert isinstance(s3, BYOSStorageDriver)
    assert isinstance(bundle, BYOSStorageDriver)


# --------------------------------------------------------------------------- #
# 1. LocalDeviceVaultDriver
# --------------------------------------------------------------------------- #


def test_local_device_vault_atomic_crud_and_quota(tmp_path: Path) -> None:
    vault_dir = tmp_path / "ext_drive_vault"
    driver = LocalDeviceVaultDriver(vault_dir, max_quota_bytes=250)
    assert driver.root_dir == vault_dir.resolve()
    assert driver.max_quota_bytes == 250
    assert driver.used_bytes() == 0

    cid_1 = "a" * 64
    cid_2 = "b" * 64
    assert not driver.has_chunk(cid_1)
    assert driver.get_chunk_mtime(cid_1) is None

    driver.put_chunk(cid_1, b"x" * 100)
    assert driver.has_chunk(cid_1)
    assert isinstance(driver.get_chunk_mtime(cid_1), float)
    assert driver.get_chunk(cid_1) == b"x" * 100
    assert driver.list_chunks() == [cid_1]
    assert driver.used_bytes() == 100

    # Stray .tmp files must be ignored by used_bytes, list_chunks, and list_manifests
    (vault_dir / "chunks" / ".stray.chk.tmp").write_bytes(b"z" * 900)
    (vault_dir / "manifests" / ".stray.json").write_bytes(b"{}")
    assert driver.used_bytes() == 100
    assert driver.list_chunks() == [cid_1]
    assert driver.list_manifests() == []

    # Overwriting existing chunk reclaims previous 100B before checking 250B quota
    driver.put_chunk(cid_1, b"y" * 180)
    assert driver.get_chunk(cid_1) == b"y" * 180
    assert driver.used_bytes() == 180

    # Exceeding 250B quota fails closed with VaultQuotaExceededError without writing
    with pytest.raises(VaultQuotaExceededError):
        driver.put_chunk(cid_2, b"q" * 80)
    assert not driver.has_chunk(cid_2)

    driver.put_manifest("snap-001", b'{"ok":true}')
    assert driver.get_manifest("snap-001") == b'{"ok":true}'
    assert driver.list_manifests() == ["snap-001"]

    with pytest.raises(VaultQuotaExceededError):
        driver.put_manifest("snap-002", b"m" * 100)

    driver.delete_chunk(cid_1)
    assert not driver.has_chunk(cid_1)
    driver.delete_manifest("snap-001")
    assert driver.list_manifests() == []


def test_local_device_vault_validation_and_error_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(PersonalVaultError):
        LocalDeviceVaultDriver(tmp_path / "bad_q", max_quota_bytes=0)

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    sym_dir = tmp_path / "sym_root"
    sym_dir.symlink_to(real_dir)
    with pytest.raises(VaultTransportError):
        LocalDeviceVaultDriver(sym_dir)

    # Unbound quota path + missing chunk/manifest + invalid IDs
    driver = LocalDeviceVaultDriver(tmp_path / "unlimited")
    driver.put_chunk("0" * 64, b"ok")
    with pytest.raises(VaultTransportError):
        driver.get_chunk("f" * 64)
    with pytest.raises(VaultTransportError):
        driver.get_manifest("missing-snap")
    with pytest.raises(PersonalVaultError):
        driver.has_chunk("../escape")
    with pytest.raises(PersonalVaultError):
        driver.put_manifest("bad..snap", b"{}")

    # Failing os.replace cleans up .tmp and raises VaultTransportError
    def _boom_replace(_src: Any, _dst: Any) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr("os.replace", _boom_replace)
    with pytest.raises(VaultTransportError):
        driver.put_chunk("1" * 64, b"payload")
    leftover_tmps = list((driver.root_dir / "chunks").glob("*.tmp"))
    assert leftover_tmps == []


def test_local_driver_io_error_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = LocalDeviceVaultDriver(tmp_path / "io_errs")
    cid = "2" * 64
    driver.put_chunk(cid, b"data")
    driver.put_manifest("s1", b"{}")

    monkeypatch.setattr(
        Path, "read_bytes", lambda _self: (_ for _ in ()).throw(OSError("read err"))
    )
    with pytest.raises(VaultTransportError):
        driver.get_chunk(cid)
    with pytest.raises(VaultTransportError):
        driver.get_manifest("s1")

    monkeypatch.undo()
    monkeypatch.setattr(
        Path, "unlink", lambda _self, missing_ok=False: (_ for _ in ()).throw(OSError("unlink err"))
    )
    with pytest.raises(VaultTransportError):
        driver.delete_chunk(cid)
    with pytest.raises(VaultTransportError):
        driver.delete_manifest("s1")

    monkeypatch.undo()
    # mkdir failure on initialization
    blocker_file = tmp_path / "blocker_file"
    blocker_file.write_bytes(b"not-a-dir")
    with pytest.raises(VaultTransportError):
        LocalDeviceVaultDriver(blocker_file / "nested")


# --------------------------------------------------------------------------- #
# 2. S3CompatibleSigV4Driver & AWS4-HMAC-SHA256 Reference Vectors
# --------------------------------------------------------------------------- #


def test_aws_sigv4_official_reference_vectors() -> None:
    fixed_ts = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)

    # Vector 1: Official AWS S3 GET Object (Range: bytes=0-9)
    vec1 = compute_sigv4_signature(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/test.txt",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="s3",
        payload=b"",
        extra_headers={"Range": "bytes=0-9"},
        timestamp=fixed_ts,
    )
    assert vec1.signature == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert vec1.authorization_header == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )

    # Vector 2: Official AWS S3 List Objects query string canonicalization
    vec2 = compute_sigv4_signature(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/?prefix=J&max-keys=2",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="s3",
        payload=b"",
        timestamp=fixed_ts,
    )
    assert vec2.signature == "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"

    # Session token inclusion + default timestamp path
    vec_tok = compute_sigv4_signature(
        method="PUT",
        url="https://r2.example.com",
        access_key_id="AKID",
        secret_access_key="SECRET",
        region="auto",
        payload=b"hello",
        session_token="SESSION-TOK-123",
    )
    assert "x-amz-security-token" in vec_tok.authorization_header
    assert vec_tok.signed_headers["x-amz-security-token"] == "SESSION-TOK-123"

    with pytest.raises(VaultAuthenticationError):
        compute_sigv4_signature(
            method="GET",
            url="https://s3.example.com/a",
            access_key_id="",
            secret_access_key="s",
            region="us-east-1",
        )
    with pytest.raises(PersonalVaultError):
        compute_sigv4_signature(
            method="GET",
            url="ftp://s3.example.com/a",
            access_key_id="a",
            secret_access_key="s",
            region="us-east-1",
        )


class _FakeS3MemoryTransport:
    """Deterministic in-memory S3 REST transport with SigV4 verification and pagination."""

    def __init__(self, *, page_size: int = 2) -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[S3HttpRequest] = []
        self.page_size = page_size
        self.forced_status: int | None = None

    def __call__(self, request: S3HttpRequest) -> S3HttpResponse:
        self.requests.append(request)
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=")
        assert request.headers["x-amz-content-sha256"] == hashlib.sha256(request.body).hexdigest()

        if self.forced_status is not None:
            return S3HttpResponse(status_code=self.forced_status)

        parsed = urllib.parse.urlsplit(request.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))

        # Path-style `/<bucket>/<key...>` or bucket list `/<bucket>?list-type=2`
        stripped = parsed.path.lstrip("/")
        parts = stripped.split("/", 1)
        object_key = parts[1] if len(parts) > 1 else ""

        if request.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            matching = sorted(k for k in self.objects if k.startswith(prefix))
            start = int(query.get("continuation-token", "0"))
            slice_keys = matching[start : start + self.page_size]
            next_idx = start + self.page_size
            truncated = next_idx < len(matching)
            items_xml = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in slice_keys)
            tok_xml = (
                f"<NextContinuationToken>{next_idx}</NextContinuationToken>" if truncated else ""
            )
            xml_body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"
                f"{tok_xml}{items_xml}</ListBucketResult>"
            ).encode()
            return S3HttpResponse(status_code=200, body=xml_body)

        if request.method == "PUT":
            self.objects[object_key] = request.body
            return S3HttpResponse(status_code=200)
        if request.method == "HEAD":
            return S3HttpResponse(status_code=200 if object_key in self.objects else 404)
        if request.method == "GET":
            if object_key not in self.objects:
                return S3HttpResponse(status_code=404)
            return S3HttpResponse(status_code=200, body=self.objects[object_key])
        if request.method == "DELETE":
            self.objects.pop(object_key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)


def test_s3_compatible_sigv4_driver_crud_pagination_and_error_mapping() -> None:
    cfg = S3SigV4Config(
        endpoint_url="https://123456.r2.cloudflarestorage.com",
        bucket="personal-vault",
        region="auto",
        access_key_id="R2_ACCESS_KEY",
        secret_access_key="R2_SUPER_SECRET_KEY",
        session_token="R2_SESSION_TOKEN",
        prefix="user-42",
    )
    assert "R2_SUPER_SECRET_KEY" not in repr(cfg)
    assert "R2_SESSION_TOKEN" not in repr(cfg)

    fake_s3 = _FakeS3MemoryTransport(page_size=2)
    fixed_clock = lambda: datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    driver = S3CompatibleSigV4Driver(cfg, http_transport=fake_s3, clock=fixed_clock)
    assert driver.config == cfg

    c1, c2, c3 = "1" * 64, "2" * 64, "3" * 64
    assert not driver.has_chunk(c1)
    driver.put_chunk(c1, b"chunk-one")
    driver.put_chunk(c2, b"chunk-two")
    driver.put_chunk(c3, b"chunk-three")

    assert driver.has_chunk(c1)
    assert driver.get_chunk(c2) == b"chunk-two"
    # Pagination exercises 2 requests (page_size=2 for 3 items)
    assert driver.list_chunks() == [c1, c2, c3]

    driver.put_manifest("snap-1", b'{"v":1}')
    driver.put_manifest("snap-2", b'{"v":2}')
    driver.put_manifest("snap-3", b'{"v":3}')
    assert driver.get_manifest("snap-2") == b'{"v":2}'
    assert driver.list_manifests() == ["snap-1", "snap-2", "snap-3"]

    driver.delete_chunk(c2)
    assert not driver.has_chunk(c2)
    with pytest.raises(VaultTransportError):
        driver.get_chunk(c2)
    with pytest.raises(VaultTransportError):
        driver.get_manifest("snap-missing")

    # Virtual-hosted mode + empty prefix
    vhost_cfg = S3SigV4Config(
        endpoint_url="https://s3.us-east-1.amazonaws.com",
        bucket="mybucket",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="SKEY",
        prefix="",
        path_style=False,
    )
    captured: list[S3HttpRequest] = []

    def _vhost_transport(req: S3HttpRequest) -> S3HttpResponse:
        captured.append(req)
        if "list-type=2" in req.url:
            return S3HttpResponse(status_code=200, body=b"<ListBucketResult/>")
        return S3HttpResponse(status_code=200, body=b"ok")

    vhost_driver = S3CompatibleSigV4Driver(vhost_cfg, http_transport=_vhost_transport)
    vhost_driver.put_chunk(c1, b"x")
    assert vhost_driver.list_manifests() == []
    assert captured[0].url.startswith("https://mybucket.s3.us-east-1.amazonaws.com/chunks/")

    # Error status codes: 403 -> Auth, 507 -> Quota, 500 -> Transport
    fake_s3.forced_status = 403
    with pytest.raises(VaultAuthenticationError):
        driver.has_chunk(c1)

    fake_s3.forced_status = 507
    with pytest.raises(VaultQuotaExceededError):
        driver.put_chunk(c1, b"x")

    fake_s3.forced_status = 500
    with pytest.raises(VaultTransportError):
        driver.put_chunk(c1, b"x")
    with pytest.raises(VaultTransportError):
        driver.has_chunk(c1)
    with pytest.raises(VaultTransportError):
        driver.put_manifest("snap-x", b"{}")
    with pytest.raises(VaultTransportError):
        driver.list_manifests()
    with pytest.raises(VaultTransportError):
        driver.delete_chunk(c1)


def test_s3_config_xml_security_and_urllib_transport_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PersonalVaultError):
        S3SigV4Config("ftp://bad", "b", "auto", "k", "s")
    with pytest.raises(PersonalVaultError):
        S3SigV4Config("https://ok.example.com", "bad/bucket", "auto", "k", "s")
    with pytest.raises(PersonalVaultError):
        S3SigV4Config("https://ok.example.com", "bucket", "  ", "k", "s")
    with pytest.raises(VaultAuthenticationError):
        S3SigV4Config("https://ok.example.com", "bucket", "auto", "", "s")

    # XXE DOCTYPE / ENTITY / CDATA / padded-comment bypass defense (Finding F2)
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(b'<!DOCTYPE foo [<!ENTITY xxe "bad">]><ListBucketResult/>')
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(b"<unclosed")

    # Regression for F2: >2,000 bytes of XML comment padding before <!DOCTYPE / <!ENTITY
    comment_padding = b"<!-- " + (b"P" * 2400) + b" -->\n"
    padded_xxe_payload = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        + comment_padding
        + b'<!DoCtYpE ListBucketResult [<!eNtItY boom "expanded">]>\n'
        + b"<ListBucketResult><Contents><Key>&boom;</Key></Contents></ListBucketResult>"
    )
    assert len(comment_padding) > 2000
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(padded_xxe_payload)

    # Padded CDATA, standalone custom entity ref, and NUL byte are also rejected
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(
            comment_padding
            + b"<ListBucketResult><Contents><Key><![CDATA[x]]></Key></Contents></ListBucketResult>"
        )
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(
            comment_padding
            + b"<ListBucketResult><Contents><Key>&custom_ent;</Key></Contents></ListBucketResult>"
        )
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(b"<ListBucketResult>\x00</ListBucketResult>")

    # Standard predefined XML character entity (&amp;) continues to work cleanly
    keys_ok, tok_ok = _parse_s3_list_xml(
        b"<ListBucketResult><Contents><Key>p/a&amp;b.json</Key></Contents></ListBucketResult>"
    )
    assert keys_ok == ["p/a&b.json"]
    assert tok_ok is None

    # Default urllib transport unit tests (offline via monkeypatch)
    with pytest.raises(VaultTransportError):
        _default_urllib_transport(
            S3HttpRequest(method="GET", url="file:///etc/passwd", headers={}),
            timeout=1.0,
        )

    class _DummyResp:
        status = 200
        headers = {"ETag": '"abc"'}

        def __enter__(self) -> _DummyResp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"resp-body"

    monkeypatch.setattr("urllib.request.urlopen", lambda _req, timeout=0: _DummyResp())
    ok_resp = _default_urllib_transport(
        S3HttpRequest(method="PUT", url="https://s3.example.com/b/k", headers={}, body=b"hi"),
        timeout=5.0,
    )
    assert ok_resp.status_code == 200
    assert ok_resp.body == b"resp-body"

    def _raise_http_err(_req: object, timeout: float = 0) -> object:
        raise urllib.error.HTTPError(
            url="https://s3.example.com/b/k",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"missing"),
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise_http_err)
    nf_resp = _default_urllib_transport(
        S3HttpRequest(method="GET", url="https://s3.example.com/b/k", headers={}),
        timeout=5.0,
    )
    assert nf_resp.status_code == 404
    assert nf_resp.body == b"missing"

    def _raise_url_err(_req: object, timeout: float = 0) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise_url_err)
    with pytest.raises(VaultTransportError):
        _default_urllib_transport(
            S3HttpRequest(method="GET", url="https://s3.example.com/b/k", headers={}),
            timeout=5.0,
        )


# --------------------------------------------------------------------------- #
# 3. PortableBundleVaultDriver (.nagarvault)
# --------------------------------------------------------------------------- #


def test_portable_bundle_vault_driver_roundtrip_quota_and_corruption(tmp_path: Path) -> None:
    with pytest.raises(PersonalVaultError):
        PortableBundleVaultDriver(tmp_path / "wrong.zip")
    with pytest.raises(PersonalVaultError):
        PortableBundleVaultDriver(tmp_path / "bad.nagarvault", max_quota_bytes=0)

    target_real = tmp_path / "real.nagarvault"
    target_real.write_bytes(b"x")
    sym_bundle = tmp_path / "sym.nagarvault"
    sym_bundle.symlink_to(target_real)
    with pytest.raises(VaultTransportError):
        PortableBundleVaultDriver(sym_bundle)

    bundle_file = tmp_path / "offline_transfer.nagarvault"
    writer = PortableBundleVaultDriver(bundle_file, max_quota_bytes=4096)
    assert writer.bundle_path == bundle_file.resolve()

    c1, c2 = "a" * 64, "b" * 64
    assert not writer.has_chunk(c1)
    writer.put_chunk(c1, b"encrypted-chunk-1")
    writer.put_chunk(c2, b"encrypted-chunk-2")
    writer.put_manifest("snap-100", b'{"snapshot":"100"}')

    with pytest.raises(VaultQuotaExceededError):
        writer.put_chunk("c" * 64, b"Q" * 5000)

    # Re-open on a "second offline device" -> full binary verification runs automatically
    reader = PortableBundleVaultDriver(bundle_file)
    assert reader.list_chunks() == [c1, c2]
    assert reader.list_manifests() == ["snap-100"]
    assert reader.get_chunk(c1) == b"encrypted-chunk-1"
    assert reader.get_manifest("snap-100") == b'{"snapshot":"100"}'

    reader.delete_chunk(c2)
    reader.delete_chunk(c2)  # idempotent delete of already-absent chunk
    assert reader.list_chunks() == [c1]
    with pytest.raises(VaultTransportError):
        reader.get_chunk(c2)
    with pytest.raises(VaultTransportError):
        reader.get_manifest("missing")

    # Parent creation error and read_bytes I/O error
    file_blocker = tmp_path / "blocker_file_bundle"
    file_blocker.write_bytes(b"not-a-dir")
    with pytest.raises(VaultTransportError):
        PortableBundleVaultDriver(file_blocker / "sub.nagarvault")

    # Corrupt a single byte in the .nagarvault file -> VaultTamperDetectedError
    raw = bytearray(bundle_file.read_bytes())
    raw[-5] ^= 0x01
    bundle_file.write_bytes(bytes(raw))
    with pytest.raises(VaultTamperDetectedError):
        PortableBundleVaultDriver(bundle_file)


def test_portable_bundle_binary_format_tamper_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1) Missing file when verify_bundle called directly, and OSError on read_bytes
    empty_drv = PortableBundleVaultDriver(tmp_path / "not_yet_written.nagarvault")
    with pytest.raises(VaultTransportError):
        empty_drv.verify_bundle()

    io_drv = PortableBundleVaultDriver(tmp_path / "io_err.nagarvault")
    io_drv.put_chunk("a" * 64, b"ok")
    monkeypatch.setattr(
        Path, "read_bytes", lambda _self: (_ for _ in ()).throw(OSError("bundle read failed"))
    )
    with pytest.raises(VaultTransportError):
        io_drv.verify_bundle()
    monkeypatch.undo()

    def _write_and_expect_tamper(name: str, payload: bytes) -> None:
        p = tmp_path / f"{name}.nagarvault"
        p.write_bytes(payload)
        with pytest.raises(VaultTamperDetectedError):
            PortableBundleVaultDriver(p)

    # 2) Truncated header
    _write_and_expect_tamper("trunc_hdr", b"NAGAR")

    # 3) Bad magic / version
    bad_hdr = _BUNDLE_HEADER_PREFIX.pack(b"BADMAGIC", 1, 0, _BUNDLE_HEADER_TOTAL, 0)
    _write_and_expect_tamper("bad_magic", bad_hdr + hashlib.sha256(bad_hdr).digest())

    # 4) Bad index bounds
    bad_bounds = _BUNDLE_HEADER_PREFIX.pack(
        BUNDLE_MAGIC, BUNDLE_VERSION, 0, _BUNDLE_HEADER_TOTAL, 99
    )
    _write_and_expect_tamper("bad_bounds", bad_bounds + hashlib.sha256(bad_bounds).digest())

    def _pack_custom_bundle(entry_count: int, blob: bytes, index_bytes: bytes) -> bytes:
        hdr = _BUNDLE_HEADER_PREFIX.pack(
            BUNDLE_MAGIC,
            BUNDLE_VERSION,
            entry_count,
            _BUNDLE_HEADER_TOTAL + len(blob),
            len(index_bytes),
        )
        sha = hashlib.sha256(hdr + blob + index_bytes).digest()
        return hdr + sha + blob + index_bytes

    # 5) Truncated index head & truncated index tail
    _write_and_expect_tamper("trunc_idx_head", _pack_custom_bundle(1, b"", b"\x01"))
    _write_and_expect_tamper("trunc_idx_tail", _pack_custom_bundle(1, b"", b"\x01\x00\x05abc"))

    # 6) Invalid UTF-8 key in index
    bad_utf8_idx = (
        _BUNDLE_INDEX_HEAD.pack(1, 2)
        + b"\xff\xfe"
        + _BUNDLE_INDEX_TAIL.pack(_BUNDLE_HEADER_TOTAL, 0, 0, hashlib.sha256(b"").digest())
    )
    _write_and_expect_tamper("bad_utf8", _pack_custom_bundle(1, b"", bad_utf8_idx))

    # 7) Out-of-bounds blob slice
    key = ("a" * 64).encode("utf-8")
    oob_idx = (
        _BUNDLE_INDEX_HEAD.pack(1, len(key)) + key + _BUNDLE_INDEX_TAIL.pack(0, 5, 0, b"\x00" * 32)
    )
    _write_and_expect_tamper("oob_blob", _pack_custom_bundle(1, b"hello", oob_idx))

    # 8) Bad per-blob CRC32 despite valid archive SHA-256
    bad_crc_idx = (
        _BUNDLE_INDEX_HEAD.pack(1, len(key))
        + key
        + _BUNDLE_INDEX_TAIL.pack(
            _BUNDLE_HEADER_TOTAL, 5, 0xDEADBEEF, hashlib.sha256(b"hello").digest()
        )
    )
    _write_and_expect_tamper("bad_crc", _pack_custom_bundle(1, b"hello", bad_crc_idx))

    # 9) Unknown entry kind (e.g. 99)
    unk_kind_idx = (
        _BUNDLE_INDEX_HEAD.pack(99, len(key))
        + key
        + _BUNDLE_INDEX_TAIL.pack(
            _BUNDLE_HEADER_TOTAL,
            5,
            zlib.crc32(b"hello") & 0xFFFFFFFF,
            hashlib.sha256(b"hello").digest(),
        )
    )
    _write_and_expect_tamper("unk_kind", _pack_custom_bundle(1, b"hello", unk_kind_idx))

    # 10) Trailing extra bytes in index
    valid_idx = (
        _BUNDLE_INDEX_HEAD.pack(1, len(key))
        + key
        + _BUNDLE_INDEX_TAIL.pack(
            _BUNDLE_HEADER_TOTAL,
            5,
            zlib.crc32(b"hello") & 0xFFFFFFFF,
            hashlib.sha256(b"hello").digest(),
        )
        + b"extra"
    )
    _write_and_expect_tamper("trailing_idx", _pack_custom_bundle(1, b"hello", valid_idx))
    assert struct.calcsize(">8sHIQQ") == 30
