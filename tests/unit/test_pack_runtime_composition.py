"""Wave 5 — the single runtime composition for the builtin capability packs.

Before Wave 5 the runtime registry was assembled at each call site, and only the
slideshow and caption packs were composed.  Five shipped packs were therefore
reported as ``pending`` (``nexus packs list``) and the repository's own manifests
failed ``nexus packs verify`` with ``unknown_capability``.

These tests pin the properties that make the composition trustworthy:

1. **truthfulness** — every ``pack.manifest.json`` on disk has exactly one
   composition entry, with a matching ``package_id``;
2. **completeness** — the composed registry knows every declared capability, so
   every builtin pack is activatable;
3. **loudness** — an incomplete composition fails *loudly* (a duplicate operation
   raises, an un-activatable pack raises) instead of silently degrading;
4. **idempotency** — asking the runtime twice for its state does not register a
   pack twice or re-order anything;
5. **containment** — the composition does not weaken the external-pack policy:
   an unknown operation from outside still cannot be registered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.packs.manifest import PackManifestError, load_manifest
from nexus_ai_agent.creative.packs.registry import (
    PackRegistry,
    PackRegistryError,
)
from nexus_ai_agent.creative.packs.runtime import (
    COMPOSITION,
    COMPOSITION_BY_DIRECTORY,
    COMPOSITION_BY_PACKAGE_ID,
    build_pack_registry,
    build_pack_runtime,
    build_runtime_registry,
    composition_issues,
    installed_version,
    stale_capabilities,
)
from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

PACKS_DIR = Path(__file__).parents[2] / "src" / "nexus_ai_agent" / "creative" / "packs"
MANIFEST_PATHS = sorted(PACKS_DIR.glob("*/pack.manifest.json"))

#: The frozen Wave-1 catalog is the floor of every composition.
WAVE1 = build_wave1_registry().list_operations()

EXPECTED_PACKAGE_IDS = (
    "nexus.slideshow.compose",
    "nexus.language.caption",
    "nexus.edit.timeline",
    "nexus.motion.graphics",
    "nexus.audio.studio",
    "nexus.color.delivery",
)


def _manifest_dict() -> dict[str, Any]:
    return json.loads((PACKS_DIR / "slideshow" / "pack.manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. composition table ⇄ manifests on disk
# ---------------------------------------------------------------------------


def test_every_shipped_manifest_has_exactly_one_composition_entry() -> None:
    directories = sorted(path.parent.name for path in MANIFEST_PATHS)
    assert directories == sorted(entry.directory for entry in COMPOSITION)
    assert composition_issues() == ()


def test_composition_package_ids_match_the_manifests() -> None:
    for path in MANIFEST_PATHS:
        entry = COMPOSITION_BY_DIRECTORY[path.parent.name]
        assert (
            load_manifest(path).package_id
            == entry.package_id
            == COMPOSITION_BY_PACKAGE_ID[entry.package_id].package_id
        )


def test_composition_order_is_the_documented_six_pack_order() -> None:
    assert tuple(entry.package_id for entry in COMPOSITION) == EXPECTED_PACKAGE_IDS


# ---------------------------------------------------------------------------
# 2. completeness — the actual Wave-5 bug fix
# ---------------------------------------------------------------------------


def test_no_builtin_pack_has_a_pending_capability() -> None:
    """The regression guard: this was ``{'nexus.audio.studio': (…), …}`` before Wave 5."""
    assert stale_capabilities() == {}


def test_runtime_knows_every_declared_capability() -> None:
    registry = build_runtime_registry()
    known = set(registry.list_operations())
    declared = {cap for path in MANIFEST_PATHS for cap in load_manifest(path).capabilities}
    assert declared <= known


def test_runtime_operations_are_the_wave1_catalog_plus_every_manifest_capability() -> None:
    registry = build_runtime_registry()
    declared = [cap for path in MANIFEST_PATHS for cap in load_manifest(path).capabilities]
    assert set(registry.list_operations()) == set(WAVE1) | set(declared)
    assert len(registry.list_operations()) == len(WAVE1) + len(set(declared))


def test_every_builtin_pack_activates_once_the_runtime_knows_it() -> None:
    runtime = build_pack_runtime(activate=True)
    assert runtime.packs.active_packs() == sorted(EXPECTED_PACKAGE_IDS)
    assert runtime.complete is True
    for row in runtime.status():
        assert row.complete, f"{row.package_id} still pending: {row.pending}"
        assert row.active is True


def test_status_rows_are_ordered_like_the_composition_and_carry_manifest_facts() -> None:
    rows = build_pack_runtime().status()
    assert [row.package_id for row in rows] == list(EXPECTED_PACKAGE_IDS)
    slideshow = rows[0]
    assert slideshow.version == "0.2.0"
    assert slideshow.directory == "slideshow"
    assert slideshow.external_binaries == ("ffmpeg",)
    slideshow_manifest = load_manifest(PACKS_DIR / "slideshow" / "pack.manifest.json")
    assert slideshow.capabilities == tuple(slideshow_manifest.capabilities)
    assert set(slideshow.as_dict()) == {
        "package_id",
        "version",
        "directory",
        "capabilities",
        "pending_capabilities",
        "active",
        "signature_state",
        "external_binaries",
    }


# ---------------------------------------------------------------------------
# 3. loudness — incomplete compositions fail, never degrade
# ---------------------------------------------------------------------------


def test_duplicate_operation_in_the_composition_raises() -> None:
    """Composing one pack twice must raise, not silently overwrite an operation."""
    entry = COMPOSITION_BY_DIRECTORY["edit"]
    with pytest.raises(ValueError, match="duplicate operation"):
        build_runtime_registry(compositions=(entry, entry))


def test_activation_refuses_a_pack_whose_operations_are_missing() -> None:
    """With an empty composition the pack is registered but cannot be activated."""
    runtime = build_pack_runtime(compositions=())
    pending = runtime.unknown_capabilities()
    assert pending, "the slideshow pack must report its six operations as unknown"
    with pytest.raises(PackRegistryError, match="cannot activate"):
        runtime.packs.activate("nexus.slideshow.compose")
    assert runtime.complete is False


def test_activate_all_skips_incomplete_packs_and_activates_the_rest() -> None:
    runtime = build_pack_runtime(compositions=(COMPOSITION_BY_DIRECTORY["caption"],))
    activated = runtime.activate_all()
    assert [pack.package_id for pack in activated] == ["nexus.language.caption"]
    assert runtime.packs.active_packs() == ["nexus.language.caption"]
    assert "nexus.audio.studio" in runtime.unknown_capabilities()


def test_composition_issues_reports_a_pack_without_a_builder(tmp_path: Path) -> None:
    ghost = tmp_path / "ghost"
    ghost.mkdir()
    (ghost / "pack.manifest.json").write_text(json.dumps(_manifest_dict()), encoding="utf-8")

    issues = composition_issues(root=tmp_path)
    assert any("'ghost' ships a manifest but has no composition entry" in i for i in issues)


def test_composition_issues_reports_a_builder_without_a_pack(tmp_path: Path) -> None:
    issues = composition_issues(root=tmp_path)
    assert any("has no pack.manifest.json" in i for i in issues)
    assert len(issues) == len(COMPOSITION)  # every entry is missing under an empty root


# ---------------------------------------------------------------------------
# 4. idempotency and repeatability
# ---------------------------------------------------------------------------


def test_two_compositions_are_equal_and_registration_is_idempotent() -> None:
    first = build_runtime_registry()
    second = build_runtime_registry()
    assert first.list_operations() == second.list_operations()

    runtime = build_pack_runtime()
    once = [pack.package_id for pack in runtime.packs.builtin_packs()]
    runtime.register_builtin()
    runtime.register_builtin()
    twice = [pack.package_id for pack in runtime.packs.builtin_packs()]
    assert once == twice == sorted(EXPECTED_PACKAGE_IDS)


def test_installed_version_is_a_string_or_none() -> None:
    version = installed_version()
    assert version is None or isinstance(version, str)


def test_tone_library_override_is_accepted_and_ignored_by_other_packs() -> None:
    from nexus_ai_agent.creative.packs.slideshow.operations import tone_library

    registry = build_runtime_registry(tone_library=tone_library())
    assert "slideshow.compose" in registry.list_operations()


# ---------------------------------------------------------------------------
# 5. containment — external policy unchanged
# ---------------------------------------------------------------------------


def test_external_pack_with_an_unknown_operation_is_still_rejected() -> None:
    runtime = build_pack_runtime()
    payload = _manifest_dict()
    payload["package_id"] = "nexus.ghost.pack"
    payload["capabilities"] = ["ghost.render_master"]  # in-namespace, unknown to the runtime
    from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest

    # PackManifestError is the base of PackRegistryError: verification refuses the
    # external pack before it can ever be registered.
    with pytest.raises(PackManifestError, match="does not know"):
        runtime.packs.register(CapabilityPackManifest.model_validate(payload), anchor="external")


def test_wave1_catalog_is_untouched_by_the_composition() -> None:
    """The frozen Wave-1 registry must not grow as a side effect of composing packs."""
    assert build_wave1_registry().list_operations() == WAVE1
    assert isinstance(build_pack_registry(), PackRegistry)
