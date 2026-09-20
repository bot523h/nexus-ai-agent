"""Wave 2a — capability-pack manifest and verification rules.

The properties under test are the ones the TDD turns into hard rules:

* a pack is **data**: unknown keys (``post_install``, ``entrypoint``, …) must
  fail validation instead of being ignored;
* ``allow_arbitrary_native_code`` / ``allow_arbitrary_wasm_imports`` cannot be
  true (they are typed ``Literal[False]``);
* an **external** pack may not introduce an operation the runtime does not
  know; a **builtin** pack may register with pending capabilities but cannot
  be activated until they exist;
* media egress is opt-in and needs the explicit permission token;
* artifact paths must stay inside the pack and digests must be content-addressed;
* ``min_nagar_version`` is enforced against the running version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.creative.packs.manifest import (
    CapabilityPackManifest,
    PackManifestError,
    dump_manifest,
    load_manifest,
)
from nexus_ai_agent.creative.packs.registry import PackRegistry, PackRegistryError
from nexus_ai_agent.creative.packs.verify import verify_manifest, verify_manifest_file
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry, build_wave1_registry

PACKS_DIR = Path(__file__).parents[2] / "src" / "nexus_ai_agent" / "creative" / "packs"
SLIDESHOW_MANIFEST = PACKS_DIR / "slideshow" / "pack.manifest.json"

WAVE1_OPERATIONS = build_wave1_registry().list_operations()


def _manifest_dict() -> dict[str, Any]:
    return json.loads(SLIDESHOW_MANIFEST.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The real, shipped manifest
# ---------------------------------------------------------------------------


def test_slideshow_manifest_is_valid_and_truthful() -> None:
    manifest = load_manifest(SLIDESHOW_MANIFEST)

    assert manifest.package_id == "nexus.slideshow.compose"
    assert manifest.capabilities == (
        "slideshow.scan_assets",
        "slideshow.suggest_tone",
        "slideshow.score_images",
        "slideshow.compose",
        "slideshow.render",
    )
    assert manifest.compatibility.protocol_version == "nagar.command.v1"
    assert manifest.compatibility.state_schema == "nagar.state.v1"
    assert manifest.network_policy.runtime_network is False
    assert manifest.network_policy.upload_media is True  # analysis egress is declared
    assert "egress_media_optin" in manifest.permissions
    assert manifest.external_binaries == ("ffmpeg",)
    assert manifest.security.allow_arbitrary_native_code is False
    assert manifest.signature_is_placeholder is True


def test_slideshow_manifest_verifies_against_the_wave1_runtime() -> None:
    report = verify_manifest_file(
        SLIDESHOW_MANIFEST,
        known_operations=WAVE1_OPERATIONS,
        current_version="3.10.0",
        anchor="builtin",
    )

    assert report.ok, report.summary()
    # Wave 2b registers these; until then they are pending, never "known".
    assert set(report.pending_capabilities) == {
        "slideshow.scan_assets",
        "slideshow.suggest_tone",
        "slideshow.score_images",
        "slideshow.compose",
        "slideshow.render",
    }
    assert report.signature_state == "placeholder"
    assert {issue.code for issue in report.warnings} == {"unsigned_manifest"}


def test_dump_manifest_round_trips() -> None:
    manifest = load_manifest(SLIDESHOW_MANIFEST)
    again = CapabilityPackManifest.model_validate(json.loads(dump_manifest(manifest)))
    assert again == manifest


# ---------------------------------------------------------------------------
# "A pack is data, not code"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_key",
    ["post_install", "entrypoint", "install", "shell", "exec", "hooks", "run"],
)
def test_unknown_manifest_keys_are_rejected(forbidden_key: str) -> None:
    payload = _manifest_dict()
    payload[forbidden_key] = "anything"
    with pytest.raises(ValidationError):
        CapabilityPackManifest.model_validate(payload)


@pytest.mark.parametrize("field", ["allow_arbitrary_native_code", "allow_arbitrary_wasm_imports"])
def test_arbitrary_code_cannot_be_requested(field: str) -> None:
    payload = _manifest_dict()
    payload["security"][field] = True
    with pytest.raises(ValidationError):
        CapabilityPackManifest.model_validate(payload)


def test_pack_must_declare_at_least_one_runtime() -> None:
    payload = _manifest_dict()
    payload["runtime"] = {"native": None, "browser": None}
    with pytest.raises(ValidationError):
        CapabilityPackManifest.model_validate(payload)


def test_missing_capabilities_are_rejected() -> None:
    payload = _manifest_dict()
    payload["capabilities"] = []
    with pytest.raises(ValidationError):
        CapabilityPackManifest.model_validate(payload)


def test_duplicate_capabilities_are_rejected() -> None:
    payload = _manifest_dict()
    payload["capabilities"] = ["slideshow.compose", "slideshow.compose"]
    with pytest.raises(ValidationError):
        CapabilityPackManifest.model_validate(payload)


def test_capability_must_live_in_the_package_namespace() -> None:
    payload = _manifest_dict()
    payload["capabilities"] = ["timeline.split_at_playhead"]
    with pytest.raises(ValidationError):
        CapabilityPackManifest.model_validate(payload)


def test_bad_package_id_and_version_are_rejected() -> None:
    payload = _manifest_dict()
    payload["package_id"] = "slideshow"
    with pytest.raises(ValidationError):
        CapabilityPackManifest.model_validate(payload)

    payload = _manifest_dict()
    payload["version"] = "1.0"
    with pytest.raises(ValidationError):
        CapabilityPackManifest.model_validate(payload)


# ---------------------------------------------------------------------------
# Artifacts and policy
# ---------------------------------------------------------------------------


def test_artifact_paths_must_stay_inside_the_pack() -> None:
    for bad_path in ("/etc/passwd", "../outside.bin", "models\\win.bin"):
        payload = _manifest_dict()
        payload["artifacts"] = [
            {
                "artifact_id": "a",
                "kind": "data",
                "path": bad_path,
                "size_mb": 1.0,
                "sha256": "sha256:" + "a" * 64,
                "license": "MIT",
            }
        ]
        with pytest.raises(ValidationError):
            CapabilityPackManifest.model_validate(payload)


def test_duplicate_artifact_paths_are_reported() -> None:
    artifact = {
        "artifact_id": "a1",
        "kind": "font",
        "path": "fonts/test.ttf",
        "size_mb": 1.0,
        "sha256": "sha256:" + "b" * 64,
        "license": "OFL-1.1",
    }
    payload = _manifest_dict()
    payload["artifacts"] = [artifact, {**artifact, "artifact_id": "a2"}]
    report = verify_manifest(CapabilityPackManifest.model_validate(payload))
    assert "duplicate_artifact_path" in {issue.code for issue in report.errors}
    assert report.ok is False


def test_placeholder_artifact_digest_is_a_warning_not_an_error() -> None:
    payload = _manifest_dict()
    payload["artifacts"] = [
        {
            "artifact_id": "mdl",
            "kind": "onnx_model",
            "path": "models/x.ort",
            "size_mb": 12.0,
            "sha256": "sha256:replace-with-real-digest",
            "license": "MIT",
        }
    ]
    report = verify_manifest(CapabilityPackManifest.model_validate(payload))
    assert report.ok is True
    assert "placeholder_artifact_digest" in {issue.code for issue in report.warnings}


def test_non_sha256_digest_is_an_error() -> None:
    payload = _manifest_dict()
    payload["artifacts"] = [
        {
            "artifact_id": "mdl",
            "kind": "onnx_model",
            "path": "models/x.ort",
            "size_mb": 12.0,
            "sha256": "md5:deadbeef",
            "license": "MIT",
        }
    ]
    report = verify_manifest(CapabilityPackManifest.model_validate(payload))
    assert "invalid_artifact_digest" in {issue.code for issue in report.errors}


def test_runtime_network_is_forbidden() -> None:
    payload = _manifest_dict()
    payload["network_policy"]["runtime_network"] = True
    report = verify_manifest(CapabilityPackManifest.model_validate(payload))
    assert "runtime_network_forbidden" in {issue.code for issue in report.errors}


def test_media_egress_requires_the_explicit_permission() -> None:
    payload = _manifest_dict()
    payload["permissions"] = [p for p in payload["permissions"] if p != "egress_media_optin"]
    report = verify_manifest(CapabilityPackManifest.model_validate(payload))
    assert "media_egress_without_permission" in {issue.code for issue in report.errors}


def test_native_adapter_network_requirement_is_rejected() -> None:
    payload = _manifest_dict()
    payload["runtime"]["native"]["network_required"] = True
    report = verify_manifest(CapabilityPackManifest.model_validate(payload))
    assert "native_runtime_network_required" in {issue.code for issue in report.errors}


def test_unknown_permission_token_is_a_warning() -> None:
    payload = _manifest_dict()
    payload["permissions"] = [*payload["permissions"], "write_everything"]
    report = verify_manifest(CapabilityPackManifest.model_validate(payload))
    assert report.ok is True
    assert "unknown_permission" in {issue.code for issue in report.warnings}


# ---------------------------------------------------------------------------
# Allow-list semantics: external vs builtin
# ---------------------------------------------------------------------------


def test_external_pack_cannot_introduce_unknown_operations() -> None:
    manifest = load_manifest(SLIDESHOW_MANIFEST)
    report = verify_manifest(manifest, known_operations=WAVE1_OPERATIONS, anchor="external")
    assert report.ok is False
    assert "unknown_capability" in {issue.code for issue in report.errors}


def test_builtin_pack_registers_with_pending_capabilities_but_cannot_activate() -> None:
    registry = PackRegistry(build_wave1_registry(), current_version="3.10.0")
    pack = registry.register_builtin(root=PACKS_DIR)[0]

    assert pack.package_id == "nexus.slideshow.compose"
    assert len(pack.pending_capabilities) == 5
    assert registry.active_packs() == []

    with pytest.raises(PackRegistryError, match="cannot activate"):
        registry.activate("nexus.slideshow.compose")


def test_activation_succeeds_once_the_runtime_knows_the_operations() -> None:
    runtime = build_wave1_registry()
    registry = PackRegistry(runtime, current_version="3.10.0")
    registry.register_builtin(root=PACKS_DIR)
    manifest = load_manifest(SLIDESHOW_MANIFEST)
    with pytest.raises(PackRegistryError, match="cannot activate"):
        registry.activate(manifest.package_id)

    # Simulate Wave 2b: the pack's own code registers its operation specs.
    from pydantic import BaseModel

    from nexus_ai_agent.creative.studio.capabilities import OperationSpec
    from nexus_ai_agent.creative.studio.models import PermissionLevel

    class _NoInput(BaseModel):
        pass

    def _noop(project: Any, context: Any) -> Any:  # pragma: no cover - never dispatched
        raise AssertionError("not executed by this test")

    for capability in manifest.capabilities:
        domain, _, name = capability.partition(".")
        runtime.register_operation(
            domain,
            "wave2",
            OperationSpec(
                operation_id=capability,
                description=f"{name} (test double)",
                permission_level=PermissionLevel.IMMEDIATE,
                input_model=_NoInput,
                handler=_noop,
            ),
        )

    assert registry.activate(manifest.package_id).active is True
    assert registry.active_packs() == ["nexus.slideshow.compose"]
    assert registry.deactivate(manifest.package_id).active is False


def test_incompatible_min_nagar_version_is_an_error() -> None:
    manifest = load_manifest(SLIDESHOW_MANIFEST)
    report = verify_manifest(manifest, current_version="3.9.0", anchor="builtin")
    assert "incompatible_nagar_version" in {issue.code for issue in report.errors}


def test_unreadable_manifest_raises_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(PackManifestError, match="not found"):
        load_manifest(tmp_path / "missing.json")

    broken = tmp_path / "pack.manifest.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(PackManifestError, match="not valid JSON"):
        load_manifest(broken)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_packs_list_reports_the_builtin_pack() -> None:
    """Wave 2b: the runtime knows the pack's operations, so nothing is pending."""
    result = CliRunner().invoke(app, ["packs", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [pack["package_id"] for pack in payload] == ["nexus.slideshow.compose"]
    assert payload[0]["pending_capabilities"] == []
    assert payload[0]["active"] is False  # activation stays an explicit step
    assert payload[0]["signature_state"] == "placeholder"
    assert payload[0]["external_binaries"] == ["ffmpeg"]


def test_cli_packs_list_human_output() -> None:
    result = CliRunner().invoke(app, ["packs", "list"])
    assert result.exit_code == 0, result.output
    assert "nexus.slideshow.compose" in result.output
    assert "capabilities=5" in result.output


def test_cli_packs_verify_accepts_the_builtin_manifest() -> None:
    """Wave 2b: the runtime knows the five operations, so verification passes."""
    result = CliRunner().invoke(app, ["packs", "verify", str(SLIDESHOW_MANIFEST), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["pending_capabilities"] == []


def test_cli_packs_verify_rejects_a_tampered_manifest(tmp_path: Path) -> None:
    payload = _manifest_dict()
    payload["network_policy"]["runtime_network"] = True
    payload["capabilities"] = ["slideshow.render_master"]  # valid namespace, unknown op
    tampered = tmp_path / "pack.manifest.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    result = CliRunner().invoke(app, ["packs", "verify", str(tampered)])
    assert result.exit_code == 1
    assert "unknown_capability" in result.output
    assert "runtime_network_forbidden" in result.output


def test_cli_packs_verify_reports_a_missing_file(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["packs", "verify", str(tmp_path / "nope.json")])
    assert result.exit_code == 1
    assert "manifest not found" in result.output


def test_pack_registry_rejects_duplicate_registration() -> None:
    registry = PackRegistry(build_wave1_registry())
    manifest = load_manifest(SLIDESHOW_MANIFEST)
    registry.register(manifest, anchor="builtin")
    with pytest.raises(PackRegistryError, match="already registered"):
        registry.register(manifest, anchor="builtin")


def test_pack_registry_unknown_pack_lookup() -> None:
    registry = PackRegistry(CapabilityRegistry())
    with pytest.raises(PackRegistryError, match="not registered"):
        registry.get("nexus.unknown")
