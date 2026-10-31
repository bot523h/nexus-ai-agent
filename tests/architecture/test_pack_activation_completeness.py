"""Architecture gate — a shipped pack can never be dead weight again.

Wave 5 fixes a *composition* defect, not a substrate defect: five of the six
builtin packs declared capabilities that the runtime could already execute, but
the registry was assembled one call site at a time, so ``nexus packs list``
reported them as ``pending`` forever and ``nexus packs verify`` rejected the
repository's own manifests.

A unit test on today's state would not stop the defect from returning.  These
gates are therefore written against the *rule*, and they fail for any future pack,
any future call site and any future drift:

1. **every** manifest on disk has exactly one composition entry (a new pack
   directory without a builder is a finding, never a silent skip);
2. **no** builtin capability is unknown to the composed runtime — the
   activation-completeness measurement must be empty;
3. every builtin pack is activatable, and its manifest verifies against the
   composed allow-list with zero pending capabilities;
4. composing is *not* shadowing: each pack's own ``build_*_registry()``
   operations stay a subset of the composed registry;
5. the CLI cannot drift from the composition module: the registry the CLI
   consumes and the registry the composition builds expose the same operations.
"""

from __future__ import annotations

import json
from pathlib import Path

from nexus_ai_agent.creative.packs.manifest import load_manifest
from nexus_ai_agent.creative.packs.runtime import (
    COMPOSITION,
    build_pack_runtime,
    build_runtime_registry,
    composition_issues,
    stale_capabilities,
)
from nexus_ai_agent.creative.packs.verify import verify_manifest

REPO_ROOT = Path(__file__).parents[2]
PACKS_DIR = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs"
BUILDERS: dict[str, tuple[str, str]] = {
    "slideshow": (
        "nexus_ai_agent.creative.packs.slideshow.operations",
        "build_slideshow_registry",
    ),
    "caption": ("nexus_ai_agent.creative.packs.caption.operations", "build_caption_registry"),
    "edit": ("nexus_ai_agent.creative.packs.edit.operations", "build_edit_registry"),
    "motion": ("nexus_ai_agent.creative.packs.motion.operations", "build_motion_registry"),
    "audio": ("nexus_ai_agent.creative.packs.audio.operations", "build_audio_registry"),
    "delivery": ("nexus_ai_agent.creative.packs.delivery.operations", "build_delivery_registry"),
    "portrait": (
        "nexus_ai_agent.creative.packs.portrait.operations",
        "build_portrait_registry",
    ),
    "scene": ("nexus_ai_agent.creative.packs.scene.operations", "build_scene_registry"),
}


def _pack_directories() -> list[str]:
    return sorted(
        path.name
        for path in PACKS_DIR.iterdir()
        if path.is_dir() and (path / "pack.manifest.json").exists()
    )


# ---------------------------------------------------------------------------
# 1. no pack directory may be silently skipped
# ---------------------------------------------------------------------------


def test_every_pack_directory_is_composed_or_the_gate_fails() -> None:
    directories = _pack_directories()
    assert directories, "no pack manifests found — the gate would be vacuous"
    assert set(directories) == {entry.directory for entry in COMPOSITION}
    assert composition_issues() == ()


def test_every_composed_pack_declares_its_builder() -> None:
    """Each pack must ship the ``register_*``/``build_*`` pair the composition calls."""
    for entry in COMPOSITION:
        module_name, function_name = BUILDERS[entry.directory]
        module = __import__(module_name, fromlist=[function_name])
        assert callable(getattr(module, function_name)), f"{module_name}:{function_name} missing"
        operations_source = (PACKS_DIR / entry.directory / "operations.py").read_text(
            encoding="utf-8"
        )
        assert "def register_" in operations_source, (
            f"{entry.label}: operations.py has no registrar"
        )


# ---------------------------------------------------------------------------
# 2. the measurement that must stay empty
# ---------------------------------------------------------------------------


def test_no_builtin_capability_is_unknown_to_the_composed_runtime() -> None:
    unknown = stale_capabilities()
    assert unknown == {}, f"builtin packs with unexecutable capabilities: {unknown}"


def test_composition_issues_is_the_only_authority_on_pack_consistency() -> None:
    """The gate reads the shipped verifier instead of re-implementing it."""
    assert composition_issues() == ()


# ---------------------------------------------------------------------------
# 3. activation completeness, per pack
# ---------------------------------------------------------------------------


def test_each_builtin_pack_verifies_with_zero_pending_capabilities() -> None:
    known = build_runtime_registry().list_operations()
    for path in sorted(PACKS_DIR.glob("*/pack.manifest.json")):
        report = verify_manifest(
            load_manifest(path), known_operations=known, current_version=None, anchor="builtin"
        )
        assert report.pending_capabilities == (), f"{path.parent.name}: {report.summary()}"
        assert report.ok, f"{path.parent.name}: {report.summary()}"


def test_every_builtin_pack_is_activatable_today() -> None:
    runtime = build_pack_runtime(activate=True)
    assert runtime.complete
    assert len(runtime.packs.active_packs()) == len(COMPOSITION)
    for row in runtime.status():
        assert row.active and row.complete, row


def test_raw_manifest_capabilities_are_all_registered_operations() -> None:
    """Read the manifest bytes, not the model: the gate must not trust one layer."""
    known = set(build_runtime_registry().list_operations())
    for path in sorted(PACKS_DIR.glob("*/pack.manifest.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        missing = [cap for cap in raw["capabilities"] if cap not in known]
        assert missing == [], f"{path.parent.name}: {missing} are declared but never registered"


# ---------------------------------------------------------------------------
# 4. composing must not shadow a pack's own registry
# ---------------------------------------------------------------------------


def test_pack_own_registry_operations_are_a_subset_of_the_composition() -> None:
    composed = set(build_runtime_registry().list_operations())
    for entry in COMPOSITION:
        module_name, function_name = BUILDERS[entry.directory]
        module = __import__(module_name, fromlist=[function_name])
        own = set(getattr(module, function_name)().list_operations())
        assert own <= composed, f"{entry.label}: {sorted(own - composed)} lost in composition"


# ---------------------------------------------------------------------------
# 5. the CLI cannot drift from the composition module
# ---------------------------------------------------------------------------


def test_cli_registry_is_the_composition_registry() -> None:
    """A future call site must not go back to hand-assembling the registry."""
    from nexus_ai_agent.cli import _packs_registry

    cli_operations = set(_packs_registry().runtime_registry.list_operations())
    assert cli_operations == set(build_runtime_registry().list_operations())


# ---------------------------------------------------------------------------
# 6. the measurement tool must not loosen the substrate's data-only gate
# ---------------------------------------------------------------------------


def test_coverage_harness_stays_outside_the_data_only_substrate() -> None:
    """Wave 5 measures pack coverage *without* relaxing the substrate allowlist.

    ``tests/architecture/test_pack_manifest_is_data_only.py`` whitelists the
    substrate's imports; the harness needs ``trace``/``dis``/``types``/``pytest``,
    none of which belong in a data-only pack.  The harness therefore lives in
    ``nexus_ai_agent.continuum`` (repo-truth tooling) next to the snapshot
    verifier, and this gate keeps it there: a copy inside ``creative/packs/``
    would either be dead code or force the allowlist open.
    """
    harness = REPO_ROOT / "src" / "nexus_ai_agent" / "continuum" / "pack_coverage.py"
    assert harness.is_file()
    harness_imports = {
        line.split()[1]
        for line in harness.read_text(encoding="utf-8").splitlines()
        if line.startswith("import ")
    }
    assert {"trace", "dis", "types"} <= harness_imports
    assert not (PACKS_DIR / "coverage.py").exists()
    assert not (PACKS_DIR / "pack_coverage.py").exists()


def test_pack_coverage_cli_is_a_thin_script_over_the_installed_package() -> None:
    """``scripts/`` is not installed: the logic must be importable from the package."""
    script = REPO_ROOT / "scripts" / "pack_coverage.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "from nexus_ai_agent.continuum.pack_coverage import" in text
    from nexus_ai_agent.continuum.pack_coverage import measure as harness_measure

    assert callable(harness_measure)
