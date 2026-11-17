"""F2 — the fail-closed signature policy must be *reachable*, not just correct.

`verify_installed_pack` refusing to run without a trust root only protects
anyone if something calls it in production.  These tests cover the consumer
(`storage.resource_packs.delivery`) and the route that exposes it
(`GET /api/studio/resource-packs`), including the state that matters most: a
perfectly intact pack that simply cannot be authenticated.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from resource_pack_testkit import PackMirror, blob, core_seed_entries

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from nexus_ai_agent.storage.resource_packs import (  # noqa: E402
    InstalledPackStatus,
    LocalDirectoryPackSource,
    ResourcePackSyncManager,
    delivery_report,
    delivery_statuses,
    sign_manifest,
)


def _keypair() -> tuple[bytes, bytes]:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


def _installed_packs(mirror: PackMirror, root: Path, *, sign: bool = True) -> bytes:
    """Install one signed-or-unsigned pack at the versioned layout; return the key."""
    private, public = _keypair()
    entry = mirror.add_remote("audio/room_tone.ogg", blob("room-tone", 4096))
    manifest = mirror.manifest("core", "1.0.0", [entry, *core_seed_entries()])
    catalog = sign_manifest(manifest, private) if sign else manifest
    manager = ResourcePackSyncManager(
        root,
        [catalog],
        LocalDirectoryPackSource(mirror.root),
        trusted_public_key=public if sign else None,
        allow_unsigned=True,
        disk_free_probe=lambda _: 10**12,
    )
    manager.sync_pack("core")
    return public


def test_a_signed_pack_reports_ready_with_the_trusted_key(
    mirror: PackMirror, tmp_path: Path
) -> None:
    public = _installed_packs(mirror, tmp_path / "device")
    rows = delivery_statuses(tmp_path / "device", trusted_public_key=public)
    assert [row.pack_id for row in rows] == ["core"]
    row = rows[0]
    assert isinstance(row, InstalledPackStatus)
    assert (row.state, row.signature) == ("ready", "verified")
    assert row.usable and row.version == "1.0.0"
    assert row.verified_files == 4  # one remote blob + three baked seeds


def test_without_a_key_the_same_intact_pack_is_withheld_not_called_ready(
    mirror: PackMirror, tmp_path: Path
) -> None:
    """The heart of the change: intact bytes + no trust root ≠ deliverable."""
    public = _installed_packs(mirror, tmp_path / "device")
    rows = delivery_statuses(tmp_path / "device")
    assert len(rows) == 1
    assert rows[0].state == "signature_required"
    assert rows[0].signature == "not_checked"
    assert not rows[0].usable
    assert "trusted_public_key is required" in rows[0].detail
    # And the keyed call proves the bytes themselves were never the problem.
    assert delivery_statuses(tmp_path / "device", trusted_public_key=public)[0].state == "ready"


def test_the_explicit_unsigned_policy_reports_unsigned_allowed(
    mirror: PackMirror, tmp_path: Path
) -> None:
    _installed_packs(mirror, tmp_path / "device", sign=False)
    rows = delivery_statuses(tmp_path / "device", allow_unsigned=True)
    assert (rows[0].state, rows[0].signature) == ("unsigned_allowed", "unsigned_allowed")
    assert rows[0].usable
    assert "signature not required" in rows[0].detail


def test_a_tampered_install_is_repair_required_not_signature_required(
    mirror: PackMirror, tmp_path: Path
) -> None:
    """Unsigned and corrupted must never be reported as the same thing."""
    public = _installed_packs(mirror, tmp_path / "device")
    table = tmp_path / "device" / "packs" / "core" / "versions"
    install = next(path for path in table.iterdir() if path.is_dir())
    victim = install / "audio" / "room_tone.ogg"
    victim.write_bytes(victim.read_bytes() + b"tampered")
    rows = delivery_statuses(tmp_path / "device", trusted_public_key=public)
    assert rows[0].state == "repair_required"
    assert "do not match the manifest" in rows[0].detail


def test_a_broken_pointer_is_reported_without_raising(mirror: PackMirror, tmp_path: Path) -> None:
    public = _installed_packs(mirror, tmp_path / "device")
    pointer = tmp_path / "device" / "packs" / "core" / "active.json"
    pointer.write_bytes(b"{not json")
    rows = delivery_statuses(tmp_path / "device", trusted_public_key=public)
    assert rows[0].state == "repair_required"
    assert "active pointer" in rows[0].detail

    pointer.write_bytes(b"[]")
    rows = delivery_statuses(tmp_path / "device", trusted_public_key=public)
    assert rows[0].state == "repair_required"
    assert "not a JSON object" in rows[0].detail

    pointer.write_bytes(b"{}")
    rows = delivery_statuses(tmp_path / "device", trusted_public_key=public)
    assert rows[0].state == "repair_required"
    assert "does not name a version" in rows[0].detail


def test_an_unreadable_installed_manifest_is_reported_as_repair_required(
    mirror: PackMirror, tmp_path: Path
) -> None:
    public = _installed_packs(mirror, tmp_path / "device")
    versions_dir = tmp_path / "device" / "packs" / "core" / "versions"
    manifest_file = next(versions_dir.glob("*.manifest.json"))
    manifest_file.unlink()
    rows = delivery_statuses(tmp_path / "device", trusted_public_key=public)
    assert rows[0].state == "repair_required"
    assert "installed manifest is unreadable" in rows[0].detail


def test_an_unconfigured_root_is_not_an_empty_success(mirror: PackMirror, tmp_path: Path) -> None:
    report = delivery_report(None)
    assert report["configured"] is False and report["fail_closed"] is True
    assert report["packs"] == [] and report["reason"]
    empty = delivery_report(tmp_path / "nothing-here")
    assert empty["configured"] is True and empty["packs"] == []


# ── the route: the policy is observable over HTTP ────────────────────────────


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "dashboard-secret-0123456789")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    from nexus_ai_agent.api import app as app_module

    yield TestClient(app_module.app, base_url="https://testserver")
    settings_module.get_settings.cache_clear()


def _login(client: TestClient) -> None:
    from nexus_ai_agent.api.studio_session import CSRF_HEADER, CSRF_VALUE

    response = client.post(
        "/studio/session",
        json={"token": "dashboard-secret-0123456789"},
        headers={CSRF_HEADER: CSRF_VALUE},
    )
    assert response.status_code == 200, response.text


def test_the_route_requires_a_session(client: TestClient) -> None:
    assert client.get("/api/studio/resource-packs").status_code == 401


def test_the_route_reports_fail_closed_without_a_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mirror: PackMirror, tmp_path: Path
) -> None:
    device = tmp_path / "device"
    _installed_packs(mirror, device)
    monkeypatch.setenv("NEXUS_RESOURCE_PACK_ROOT", str(device))
    monkeypatch.delenv("NEXUS_RESOURCE_PACK_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("NEXUS_RESOURCE_PACK_ALLOW_UNSIGNED", raising=False)
    _login(client)
    body = client.get("/api/studio/resource-packs").json()
    assert body["configured"] is True and body["fail_closed"] is True
    assert [row["state"] for row in body["packs"]] == ["signature_required"]
    assert body["packs"][0]["usable"] is False
    assert client.get("/api/studio/resource-packs").headers["cache-control"] == "no-store"


def test_the_route_verifies_with_the_configured_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mirror: PackMirror, tmp_path: Path
) -> None:
    device = tmp_path / "device"
    public = _installed_packs(mirror, device)
    monkeypatch.setenv("NEXUS_RESOURCE_PACK_ROOT", str(device))
    monkeypatch.setenv("NEXUS_RESOURCE_PACK_PUBLIC_KEY", public.hex())
    _login(client)
    body = client.get("/api/studio/resource-packs").json()
    assert body["fail_closed"] is True
    assert body["packs"][0]["state"] == "ready"
    assert body["packs"][0]["signature"] == "verified"


def test_the_route_never_lets_an_env_typo_weaken_the_policy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mirror: PackMirror, tmp_path: Path
) -> None:
    """A malformed key is treated as "no key", never as "no signature needed"."""
    device = tmp_path / "device"
    _installed_packs(mirror, device)
    monkeypatch.setenv("NEXUS_RESOURCE_PACK_ROOT", str(device))
    for bad in ("zzzz", "00" * 31):
        monkeypatch.setenv("NEXUS_RESOURCE_PACK_PUBLIC_KEY", bad)
        _login(client)
        body = client.get("/api/studio/resource-packs").json()
        assert body["fail_closed"] is True
        assert body["packs"][0]["state"] == "signature_required"


def test_the_route_reports_the_unsigned_policy_only_when_asked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mirror: PackMirror, tmp_path: Path
) -> None:
    device = tmp_path / "device"
    _installed_packs(mirror, device, sign=False)
    monkeypatch.setenv("NEXUS_RESOURCE_PACK_ROOT", str(device))
    monkeypatch.delenv("NEXUS_RESOURCE_PACK_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("NEXUS_RESOURCE_PACK_ALLOW_UNSIGNED", "1")
    _login(client)
    body = client.get("/api/studio/resource-packs").json()
    assert body["fail_closed"] is False
    assert body["packs"][0]["state"] == "unsigned_allowed"


def test_the_capability_table_states_the_limit_of_the_route() -> None:
    from nexus_ai_agent.api import studio_web as web

    rows = {row["id"]: row for row in web.CAPABILITIES}
    assert rows["resource_packs"]["status"] == "VERIFIED_WITH_LIMITATIONS"
    assert "read-only" in rows["resource_packs"]["reason"]
    assert Path(rows["resource_packs"]["evidence"]).is_file()


def test_a_pack_with_no_pointer_directory_is_not_invented(
    mirror: PackMirror, tmp_path: Path
) -> None:
    _installed_packs(mirror, tmp_path / "device")
    (tmp_path / "device" / "packs" / "ghost").mkdir()
    assert [row.pack_id for row in delivery_statuses(tmp_path / "device")] == ["core"]
    assert json.dumps(delivery_report(tmp_path / "device"))


def test_missing_cryptography_fails_closed_across_delivery_and_manifest(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from nexus_ai_agent.storage.resource_packs import (
        SignatureBackendUnavailableError,
        manifest_from_json,
        sign_manifest,
        verify_installed_pack,
        verify_manifest_signature,
    )

    device = tmp_path / "device"
    public = _installed_packs(mirror, device)
    pack_dir = device / "packs" / "core"
    pointer = json.loads((pack_dir / "active.json").read_text(encoding="utf-8"))
    install_name = pointer["install_dir"]
    manifest = manifest_from_json(
        (pack_dir / "versions" / f"{install_name}.manifest.json").read_bytes()
    )
    install_dir = pack_dir / "versions" / install_name

    real_import = importlib.import_module

    def without_cryptography(name: str, package: str | None = None) -> object:
        if name.startswith("cryptography"):
            raise ImportError(name)
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", without_cryptography)

    # pack_manifest.py: signing and signature verification fail closed
    with pytest.raises(SignatureBackendUnavailableError, match="cryptography"):
        sign_manifest(manifest, b"\x01" * 32)
    with pytest.raises(SignatureBackendUnavailableError, match="cryptography"):
        verify_manifest_signature(manifest, public)
    with pytest.raises(SignatureBackendUnavailableError, match="cryptography"):
        verify_installed_pack(manifest, install_dir, trusted_public_key=public)

    # delivery.py: delivery_statuses and delivery_report withhold the pack as signature_required
    statuses = delivery_statuses(device, trusted_public_key=public)
    assert len(statuses) == 1
    assert statuses[0].state == "signature_required"
    assert statuses[0].signature == "not_checked"
    assert statuses[0].verified_files == 0
    assert "cryptography" in statuses[0].detail

    report = delivery_report(device, trusted_public_key=public)
    assert report["configured"] is True
    assert report["fail_closed"] is True
    assert report["packs"][0]["state"] == "signature_required"
    assert "cryptography" in report["packs"][0]["detail"]

    # Explicit opt-out policy still respects allow_unsigned=True without silent key verification
    unsigned_statuses = delivery_statuses(device, allow_unsigned=True)
    assert unsigned_statuses[0].state == "unsigned_allowed"
