"""The pack trust plane: claim ≠ evidence ≠ authority ≠ activation.

Every test here is written from the attacker's side: it tries to make an
untrusted pack look trusted and asserts that it cannot.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from ed25519_testkit import (
    IDENTITY_PUBLIC_KEY,
    forge_for_identity_key,
    generate_keypair,
    malleate,
    sign,
)

from nexus_ai_agent.creative.packs.ed25519 import Ed25519Error, verify_ed25519
from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest
from nexus_ai_agent.creative.packs.registry import PackRegistry, PackRegistryError
from nexus_ai_agent.creative.packs.trust import (
    DEFAULT_TRUST_ROOT_PATH,
    TrustRoot,
    TrustRootError,
    TrustState,
    canonical_signing_bytes,
    evaluate_trust,
)
from nexus_ai_agent.creative.packs.verify import verify_manifest
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry

PUBLISHER = "acme-labs"


# --------------------------------------------------------------- fixtures --


def _manifest_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "manifest_schema": "nexus.capability-pack.v1",
        "package_id": "nexus.demo.pack",
        "version": "1.0.0",
        "display_name": "Demo",
        "download_size_mb": 1.5,
        "capabilities": ["demo.one"],
        "runtime": {"native": {"adapter_id": "demo", "sandbox": "process_isolated"}},
        "permissions": ["read_project_images"],
        "hardware_requirements": {
            "minimum_ram_mb": 512,
            "recommended_ram_mb": 1024,
            "minimum_cpu_threads": 1,
            "disk_free_mb": 10,
        },
        "resource_budget": {
            "max_resident_model_mb": 64,
            "max_audio_minutes_in_memory": 5.0,
            "max_preview_resolution": "1280x720",
        },
        "compatibility": {"min_nagar_version": "0.1.0"},
        "security": {
            "signature_algorithm": "ed25519",
            "signature": "base64:replace-with-signed-manifest",
            "trusted_publisher": PUBLISHER,
        },
    }
    payload.update(overrides)
    return payload


def _manifest(**overrides: Any) -> CapabilityPackManifest:
    return CapabilityPackManifest.model_validate(_manifest_payload(**overrides))


def _sign_manifest(manifest: CapabilityPackManifest, keypair: Any) -> CapabilityPackManifest:
    signature = sign(keypair, canonical_signing_bytes(manifest))
    payload = manifest.model_dump(mode="json")
    payload["security"]["signature"] = "ed25519:" + signature.hex()
    return CapabilityPackManifest.model_validate(payload)


def _trust_root(*entries: tuple[str, str, str], publisher: str = PUBLISHER) -> TrustRoot:
    return TrustRoot.from_mapping(
        {
            "schema": "nexus.pack-trust-root.v1",
            "version": 1,
            "threshold": 1,
            "publishers": {
                publisher: [
                    {"key_id": key_id, "public_key": public_hex, "status": status}
                    for key_id, public_hex, status in entries
                ]
            },
        },
        source="<test>",
    )


@pytest.fixture()
def keypair() -> Any:
    return generate_keypair()


@pytest.fixture()
def signed(keypair: Any) -> CapabilityPackManifest:
    return _sign_manifest(_manifest(), keypair)


@pytest.fixture()
def root(keypair: Any) -> TrustRoot:
    return _trust_root(("acme-1", keypair.public_hex, "active"))


# ------------------------------------------------- the happy path exists ---


def test_a_signature_from_a_trusted_key_verifies(
    signed: CapabilityPackManifest, root: TrustRoot
) -> None:
    decision = evaluate_trust(signed, root)
    assert decision.state is TrustState.VERIFIED
    assert decision.trusted is True
    assert decision.key_id == "acme-1"


# ---------------------------------------------------- claim ≠ authority ----


def test_manifest_cannot_nominate_its_own_publisher(
    signed: CapabilityPackManifest, keypair: Any
) -> None:
    """The same valid signature, but the trust root knows a different name."""
    other_root = _trust_root(("acme-1", keypair.public_hex, "active"), publisher="someone-else")
    decision = evaluate_trust(signed, other_root)
    assert decision.state is TrustState.UNKNOWN_PUBLISHER
    assert decision.trusted is False


def test_an_attacker_key_is_not_trusted_just_because_it_signed(
    root: TrustRoot,
) -> None:
    attacker = generate_keypair()
    forged = _sign_manifest(_manifest(), attacker)
    decision = evaluate_trust(forged, root)
    assert decision.state is TrustState.SIGNATURE_INVALID


def test_no_trust_root_is_not_a_pass(signed: CapabilityPackManifest) -> None:
    assert evaluate_trust(signed, None).state is TrustState.NO_TRUST_ROOT


def test_revoked_key_is_reported_as_revoked_not_as_invalid(
    signed: CapabilityPackManifest, keypair: Any
) -> None:
    decision = evaluate_trust(signed, _trust_root(("acme-1", keypair.public_hex, "revoked")))
    assert decision.state is TrustState.NO_TRUSTED_KEYS
    decision2 = evaluate_trust(
        signed,
        _trust_root(
            ("acme-1", keypair.public_hex, "revoked"),
            ("acme-2", generate_keypair().public_hex, "active"),
        ),
    )
    assert decision2.state is TrustState.REVOKED_KEY
    assert decision2.trusted is False


def test_placeholder_and_malformed_signatures_are_distinct_states(root: TrustRoot) -> None:
    assert evaluate_trust(_manifest(), root).state is TrustState.PLACEHOLDER
    for bogus in (
        "ed25519:not-hex" + "0" * 120,
        "ed25519:" + "ab" * 63,
        "totally-bogus",
        "base64:" + "A" * 86,
    ):
        payload = _manifest_payload()
        payload["security"]["signature"] = bogus
        state = evaluate_trust(CapabilityPackManifest.model_validate(payload), root).state
        assert state in (TrustState.MALFORMED_SIGNATURE, TrustState.PLACEHOLDER), bogus


# ------------------------------------------------- canonical bytes rules ---


TAMPERS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("version", {"version": "1.0.1"}),
    ("capabilities", {"capabilities": ["demo.one", "demo.two"]}),
    ("permissions", {"permissions": ["read_project_images", "egress_media_optin"]}),
    ("display_name", {"display_name": "Demo!"}),
    ("download_size_mb", {"download_size_mb": 1.6}),
    ("package_id", {"package_id": "nexus.demo.other"}),
    ("compatibility", {"compatibility": {"min_nagar_version": "9.9.9"}}),
    (
        "network_policy",
        {"network_policy": {"upload_media": True, "pack_update": "optional_signed_download"}},
    ),
    (
        "artifacts",
        {
            "artifacts": [
                {
                    "artifact_id": "m1",
                    "kind": "onnx_model",
                    "path": "models/m.onnx",
                    "size_mb": 1.0,
                    "sha256": "sha256:" + "aa" * 32,
                    "license": "MIT",
                }
            ]
        },
    ),
    (
        "runtime",
        {"runtime": {"native": {"adapter_id": "other", "sandbox": "process_isolated"}}},
    ),
)


@pytest.mark.parametrize("field,override", TAMPERS, ids=[name for name, _ in TAMPERS])
def test_one_changed_field_breaks_the_signature(
    keypair: Any, root: TrustRoot, field: str, override: dict[str, Any]
) -> None:
    signed = _sign_manifest(_manifest(), keypair)
    payload = signed.model_dump(mode="json")
    payload.update(override)
    tampered = CapabilityPackManifest.model_validate(payload)
    assert canonical_signing_bytes(tampered) != canonical_signing_bytes(signed)
    assert evaluate_trust(tampered, root).state is TrustState.SIGNATURE_INVALID


def test_publisher_is_covered_by_the_signature(keypair: Any) -> None:
    signed = _sign_manifest(_manifest(), keypair)
    payload = signed.model_dump(mode="json")
    payload["security"]["trusted_publisher"] = "someone-else"
    swapped = CapabilityPackManifest.model_validate(payload)
    root = _trust_root(("acme-1", keypair.public_hex, "active"), publisher="someone-else")
    # The key is trusted *for that publisher*, but the bytes no longer match.
    assert evaluate_trust(swapped, root).state is TrustState.SIGNATURE_INVALID


def test_canonical_bytes_are_stable_across_key_order_and_are_pure_ascii() -> None:
    a = CapabilityPackManifest.model_validate(_manifest_payload())
    shuffled = dict(reversed(list(_manifest_payload().items())))
    b = CapabilityPackManifest.model_validate(shuffled)
    assert canonical_signing_bytes(a) == canonical_signing_bytes(b)
    canonical_signing_bytes(a).decode("ascii")  # must not raise
    unicode_name = _manifest_payload(display_name="Démo ✨")
    blob = canonical_signing_bytes(CapabilityPackManifest.model_validate(unicode_name))
    blob.decode("ascii")


def test_signature_field_is_excluded_from_its_own_canonical_bytes(keypair: Any) -> None:
    unsigned = _manifest()
    signed = _sign_manifest(unsigned, keypair)
    assert canonical_signing_bytes(unsigned) == canonical_signing_bytes(signed)
    assert b'"signature"' not in canonical_signing_bytes(signed)
    assert canonical_signing_bytes(signed).startswith(b"nexus.capability-pack.signature.v1\n")


# ------------------------------------------------------- Ed25519 itself ----


def test_rfc8032_vector_one() -> None:
    verify_ed25519(
        bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"),
        b"",
        bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590"
            "a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        ),
    )


def test_rfc8032_vector_two() -> None:
    verify_ed25519(
        bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"),
        bytes.fromhex("72"),
        bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e"
            "15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
        ),
    )


def test_malleable_signature_is_rejected(keypair: Any) -> None:
    message = b"payload"
    signature = sign(keypair, message)
    verify_ed25519(keypair.public_key, message, signature)
    with pytest.raises(Ed25519Error, match="canonical"):
        verify_ed25519(keypair.public_key, message, malleate(signature))


def test_small_order_public_key_is_rejected_even_for_a_satisfying_signature() -> None:
    """The forged signature satisfies the curve equation; the key must still lose."""
    forged = forge_for_identity_key()
    for message in (b"", b"m", b"transfer everything"):
        with pytest.raises(Ed25519Error, match="small order"):
            verify_ed25519(IDENTITY_PUBLIC_KEY, message, forged)


def test_a_small_order_publisher_key_cannot_be_installed_or_trusted() -> None:
    root = _trust_root(("weak", IDENTITY_PUBLIC_KEY.hex(), "active"))
    payload = _manifest_payload()
    payload["security"]["signature"] = "ed25519:" + forge_for_identity_key().hex()
    manifest = CapabilityPackManifest.model_validate(payload)
    assert evaluate_trust(manifest, root).state is TrustState.SIGNATURE_INVALID


def test_wrong_sizes_are_rejected(keypair: Any) -> None:
    with pytest.raises(Ed25519Error):
        verify_ed25519(b"short", b"m", sign(keypair, b"m"))
    with pytest.raises(Ed25519Error):
        verify_ed25519(keypair.public_key, b"m", b"short")


# ------------------------------------------------------- the trust root ----


def test_shipped_trust_root_contains_no_keys_and_no_secrets() -> None:
    raw = json.loads(DEFAULT_TRUST_ROOT_PATH.read_text(encoding="utf-8"))
    assert raw["publishers"] == {}
    assert TrustRoot.load().is_empty is True
    # No key material of any kind, and nothing that could be a 32-byte secret.
    text = DEFAULT_TRUST_ROOT_PATH.read_text(encoding="utf-8")
    assert not re.search(r"[0-9a-fA-F]{64}", text)
    assert "private_key" not in text and "seed" not in text


def test_broken_trust_root_raises_instead_of_silently_trusting_nothing(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "root.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(TrustRootError):
        TrustRoot.load(broken)
    with pytest.raises(TrustRootError):
        TrustRoot.load(tmp_path / "missing.json")


def test_the_trust_root_cannot_be_swapped_through_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env var that redirects the root of trust would be a downgrade switch."""
    attacker_root = tmp_path / "attacker.json"
    attacker_root.write_text(
        json.dumps(
            {
                "schema": "nexus.pack-trust-root.v1",
                "version": 1,
                "publishers": {PUBLISHER: []},
            }
        ),
        encoding="utf-8",
    )
    for name in ("NEXUS_PACK_TRUST_ROOT", "PACK_TRUST_ROOT", "TRUST_ROOT"):
        monkeypatch.setenv(name, str(attacker_root))
    assert TrustRoot.load().source == str(DEFAULT_TRUST_ROOT_PATH)
    source = (
        Path(__file__).resolve().parents[2] / "src/nexus_ai_agent/creative/packs/trust.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "os" not in imported
    assert "getenv" not in source


def test_unusable_trust_root_is_an_error_on_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misconfigured authority must be loud, never a silent downgrade."""
    broken = tmp_path / "root.json"
    broken.write_text('{"schema": "wrong"}', encoding="utf-8")
    monkeypatch.setattr("nexus_ai_agent.creative.packs.trust.DEFAULT_TRUST_ROOT_PATH", broken)
    report = verify_manifest(_manifest())  # default = load the runtime root
    assert "trust_root_unusable" in {issue.code for issue in report.errors}
    assert report.ok is False
    assert report.trusted is False


def test_trust_root_rejects_unknown_schema_threshold_and_bad_keys(tmp_path: Path) -> None:
    for raw in (
        {"schema": "other", "version": 1, "publishers": {}},
        {"schema": "nexus.pack-trust-root.v1", "version": 1, "threshold": 2, "publishers": {}},
        {
            "schema": "nexus.pack-trust-root.v1",
            "version": 1,
            "publishers": {"p": [{"key_id": "k", "public_key": "zz", "status": "active"}]},
        },
        {
            "schema": "nexus.pack-trust-root.v1",
            "version": 1,
            "publishers": {"p": [{"key_id": "k", "public_key": "aa", "status": "active"}]},
        },
        {
            "schema": "nexus.pack-trust-root.v1",
            "version": 1,
            "publishers": {"p": [{"key_id": "k", "public_key": "ab" * 32, "status": "maybe"}]},
        },
        {
            "schema": "nexus.pack-trust-root.v1",
            "version": 1,
            "publishers": {
                "p": [
                    {
                        "key_id": "k",
                        "public_key": "ab" * 32,
                        "status": "active",
                        "algorithm": "rsa",
                    }
                ]
            },
        },
    ):
        with pytest.raises(TrustRootError):
            TrustRoot.from_mapping(raw, source="<test>")


# --------------------------------------------- registry enforcement point --


def _runtime_knowing(operation: str) -> CapabilityRegistry:
    """A runtime registry that already knows ``operation`` (pack allow-list)."""
    from pydantic import BaseModel

    from nexus_ai_agent.creative.studio.capabilities import (
        OperationContext,
        OperationOutcome,
        OperationSpec,
    )
    from nexus_ai_agent.creative.studio.models import PermissionLevel, Project

    class _Input(BaseModel):
        model_config = {"extra": "forbid"}

    def _handler(project: Project, context: OperationContext) -> OperationOutcome:
        return OperationOutcome(project=project, history=context.history, output={})

    registry = CapabilityRegistry()
    domain, name = operation.split(".")
    registry.register_domain(domain, "demo")
    registry.register_operation(
        domain,
        name,
        OperationSpec(
            operation_id=operation,
            description="demo",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=_Input,
            handler=_handler,
        ),
    )
    return registry


def test_external_pack_cannot_be_activated_without_verified_signature() -> None:
    registry = PackRegistry(_runtime_knowing("demo.one"), trust_root=None)
    registry.register(_manifest(), anchor="external")
    with pytest.raises(PackRegistryError, match="not trusted"):
        registry.activate("nexus.demo.pack")
    assert registry.active_packs() == []


def test_external_pack_activates_only_through_the_trust_root(keypair: Any, root: TrustRoot) -> None:
    signed = _sign_manifest(_manifest(), keypair)
    registry = PackRegistry(_runtime_knowing("demo.one"), trust_root=root)
    pack = registry.register(signed, anchor="external")
    assert pack.report.trusted is True
    registry.activate("nexus.demo.pack")
    assert registry.active_packs() == ["nexus.demo.pack"]


def test_tampered_external_pack_is_refused_at_activation(keypair: Any, root: TrustRoot) -> None:
    signed = _sign_manifest(_manifest(), keypair)
    payload = signed.model_dump(mode="json")
    payload["permissions"] = ["read_project_images", "egress_media_optin"]
    registry = PackRegistry(_runtime_knowing("demo.one"), trust_root=root)
    registry.register(CapabilityPackManifest.model_validate(payload), anchor="external")
    with pytest.raises(PackRegistryError, match="signature_invalid"):
        registry.activate("nexus.demo.pack")


def test_builtin_packs_still_activate_and_report_their_honest_state() -> None:
    from nexus_ai_agent.creative.packs.runtime import build_pack_registry

    registry = build_pack_registry(current_version="")
    registry.register_builtin(activate=True)
    assert registry.active_packs()
    for pack in registry.builtin_packs():
        assert pack.report.trusted is False
        assert pack.report.signature_state == "placeholder"


def test_verification_reports_trust_without_granting_it(root: TrustRoot) -> None:
    report = verify_manifest(_manifest(), trust_root=root)
    assert report.ok is True  # structurally fine…
    assert report.trusted is False  # …and still not authorised
    assert "unsigned_manifest" in {issue.code for issue in report.warnings}
