"""Wave 5 — one composition for every builtin capability pack.

Before this module the runtime registry was assembled ad hoc at each call site
(``cli.py::_packs_registry`` composed the Wave-1 catalog with the *slideshow* and
*caption* packs only), so five of the six packs that ship inside this repository
were reported as ``pending`` even though their pure operations were already
importable and tested:

```
# before Wave 5
$ nexus packs list
  • nexus.audio.studio   1.0.0 — … capabilities=8  · pending=8
  • nexus.edit.timeline  1.0.0 — … capabilities=8  · pending=8
  • nexus.motion.graphics 1.0.0 — … capabilities=7 · pending=7
  • nexus.color.delivery 1.0.0 — … capabilities=7 · pending=7    # + the two clean ones
```

The substrate was never wrong; the *composition* was incomplete.  A pack may only
be activated once the runtime knows **every** operation of its manifest
(:class:`~nexus_ai_agent.creative.packs.registry.PackRegistry.activate`), so an
incomplete composition silently turned five shipped packs into dead weight — and
the ``nexus packs verify`` command rejected the repository's own manifests as
``unknown_capability``.

This module is the single, data-driven source of truth for that composition:

* :data:`COMPOSITION` — for every builtin pack, its directory, its manifest
  ``package_id`` and the registrar that adds its operations to a registry;
* :func:`build_runtime_registry` — Wave-1 catalog + every pack's operations;
* :func:`build_pack_registry` — the same registry wrapped in a
  :class:`~nexus_ai_agent.creative.packs.registry.PackRegistry`;
* :class:`PackRuntime` / :func:`build_pack_runtime` — composition plus builtin
  registration, optional activation, and a ``status`` table for reporting;
* :func:`composition_issues` — the verifier the architecture gate uses: a pack
  directory without a builder, or a builder for a directory that no longer
  exists, is a *finding*, never a silent no-op.

Design decisions (see ``docs/ops/PACK_RUNTIME.md`` for the full rationale):

1. **Composition is explicit data, not discovery magic.**  Importing a pack's
   operations is a deliberate act: a pack whose builder is missing must fail the
   gate loudly instead of being skipped.
2. **Composition ≠ activation.**  ``nexus packs list`` still reports
   ``active: false`` until ``nexus packs activate`` is called — activation stays
   an explicit, auditable step (the Wave-2 contract).  What changes is that every
   builtin pack is now *activatable*, and its manifest verifies clean against the
   runtime allow-list.
3. **No import side effects.**  Registrars are imported lazily inside
   :func:`build_runtime_registry`, so ``import nexus_ai_agent`` never drags the
   creative packs into memory, and no pack can cycle back into this module.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest
from nexus_ai_agent.creative.packs.registry import Anchor, PackRegistry, RegisteredPack
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry, build_wave1_registry

#: A pack registrar mutates the registry in place and may return it.  Both
#: shapes exist in the pack substrate (``register_*_operations`` return ``None``;
#: the slideshow and caption registrars return the registry), which is why the
#: return value is deliberately ignored by the composition.
Registrar = Callable[[CapabilityRegistry], object]


# ---------------------------------------------------------------------------
# registrars (lazy imports: the composition must not cost an import at module load)
# ---------------------------------------------------------------------------


def _register_slideshow(registry: CapabilityRegistry) -> object:
    from nexus_ai_agent.creative.packs.slideshow.operations import register_slideshow_operations

    return register_slideshow_operations(registry)


def _register_caption(registry: CapabilityRegistry) -> object:
    from nexus_ai_agent.creative.packs.caption.operations import register_caption_operations

    return register_caption_operations(registry)


def _register_edit(registry: CapabilityRegistry) -> object:
    from nexus_ai_agent.creative.packs.edit.operations import register_edit_operations

    register_edit_operations(registry)
    return registry


def _register_motion(registry: CapabilityRegistry) -> object:
    from nexus_ai_agent.creative.packs.motion.operations import register_motion_operations

    register_motion_operations(registry)
    return registry


def _register_audio(registry: CapabilityRegistry) -> object:
    from nexus_ai_agent.creative.packs.audio.operations import register_audio_operations

    register_audio_operations(registry)
    return registry


def _register_delivery(registry: CapabilityRegistry) -> object:
    from nexus_ai_agent.creative.packs.delivery.operations import register_delivery_operations

    register_delivery_operations(registry)
    return registry


def _register_portrait(registry: CapabilityRegistry) -> object:
    from nexus_ai_agent.creative.packs.portrait.operations import register_portrait_operations

    register_portrait_operations(registry)
    return registry


def _register_scene(registry: CapabilityRegistry) -> object:
    from nexus_ai_agent.creative.packs.scene.operations import register_scene_operations

    register_scene_operations(registry)
    return registry


@dataclass(frozen=True)
class PackComposition:
    """One builtin pack: where it lives, what it declares, how it is registered."""

    directory: str
    package_id: str
    register: Registrar
    summary: str

    @property
    def label(self) -> str:
        return f"{self.directory} ({self.package_id})"


#: The eight builtin packs, in registration order.  The order is deterministic and
#: part of the public contract: it is what ``nexus packs list`` prints and what
#: the activation snapshot records.  ``slideshow`` first (the pack every other
#: lane builds on), then the language, edit, motion, audio and delivery lanes,
#: and finally the vision lane (portrait → scene, task-152/153).
COMPOSITION: tuple[PackComposition, ...] = (
    PackComposition(
        "slideshow",
        "nexus.slideshow.compose",
        _register_slideshow,
        "tone-templated stills to a beat-aligned master (Wave 2b)",
    ),
    PackComposition(
        "caption",
        "nexus.language.caption",
        _register_caption,
        "local Persian/multilingual captions (Wave 4a)",
    ),
    PackComposition(
        "edit",
        "nexus.edit.timeline",
        _register_edit,
        "non-destructive timeline edits (Wave 3)",
    ),
    PackComposition(
        "motion",
        "nexus.motion.graphics",
        _register_motion,
        "transitions, keyframes, titles, parallax (Wave 6)",
    ),
    PackComposition(
        "audio",
        "nexus.audio.studio",
        _register_audio,
        "loudness, ducking, beat grid, DSP derivations (Wave 5)",
    ),
    PackComposition(
        "delivery",
        "nexus.color.delivery",
        _register_delivery,
        "colour transforms, proxies, OTIO delivery (Wave 7)",
    ),
    PackComposition(
        "portrait",
        "nexus.vision.portrait",
        _register_portrait,
        "face tracks, beauty retouch, masks, relight (task-152)",
    ),
    PackComposition(
        "scene",
        "nexus.vision.scene",
        _register_scene,
        "segmentation, tracking, semantic scene edits (task-153)",
    ),
)

#: ``directory -> composition`` for O(1) lookup by the gate and the CLI.
COMPOSITION_BY_DIRECTORY: dict[str, PackComposition] = {c.directory: c for c in COMPOSITION}

#: ``package_id -> composition``.
COMPOSITION_BY_PACKAGE_ID: dict[str, PackComposition] = {c.package_id: c for c in COMPOSITION}


class PackRuntimeError(RuntimeError):
    """The composition is incomplete or inconsistent (never raised for "pending")."""


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------


def installed_version() -> str | None:
    """The installed distribution version, or ``None`` for a source checkout.

    Single implementation for every call site (CLI, tests, docs): a wheel install
    answers the real version, a bare ``PYTHONPATH`` checkout answers ``None`` and
    the manifest verifier then skips only the ``min_nagar_version`` comparison.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("nexus-ai-agent")
    except PackageNotFoundError:  # pragma: no cover - bare source checkout
        return None


def build_runtime_registry(
    *,
    tone_library: Any | None = None,
    compositions: Iterable[PackComposition] | None = None,
) -> CapabilityRegistry:
    """Wave-1 catalog **plus every builtin pack's operations**.

    Raises :class:`~nexus_ai_agent.creative.studio.models`-level ``ValueError``
    from the registry itself if two packs claim the same operation id — the
    composition never silently overwrites an operation.
    """
    if tone_library is not None:
        from nexus_ai_agent.creative.packs.slideshow.operations import use_tone_library

        use_tone_library(tone_library)

    registry = build_wave1_registry()
    for entry in compositions if compositions is not None else COMPOSITION:
        entry.register(registry)
    return registry


def build_pack_registry(
    *,
    current_version: str | None = None,
    tone_library: Any | None = None,
    compositions: Iterable[PackComposition] | None = None,
) -> PackRegistry:
    """A :class:`PackRegistry` over the full runtime composition.

    ``current_version`` defaults to :func:`installed_version`; pass ``""``
    explicitly to opt out of the version comparison entirely.
    """
    registry = build_runtime_registry(tone_library=tone_library, compositions=compositions)
    version = installed_version() if current_version is None else current_version
    return PackRegistry(registry, current_version=version or None)


@dataclass(frozen=True)
class PackStatus:
    """One row of ``nexus packs list``: declared capability vs. known operation."""

    package_id: str
    version: str
    directory: str
    capabilities: tuple[str, ...]
    pending: tuple[str, ...]
    active: bool
    signature_state: str
    external_binaries: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """The runtime knows every capability the manifest declares."""
        return not self.pending

    def as_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "version": self.version,
            "directory": self.directory,
            "capabilities": list(self.capabilities),
            "pending_capabilities": list(self.pending),
            "active": self.active,
            "signature_state": self.signature_state,
            "external_binaries": list(self.external_binaries),
        }


@dataclass
class PackRuntime:
    """Composition + registered builtin packs, with reporting and activation.

    ``composition`` is carried on the instance (never read from the module global
    inside a method) so a runtime built for a subset of packs — as the tests and
    the completeness gate do — reports and activates exactly that subset.
    """

    registry: CapabilityRegistry
    packs: PackRegistry
    composition: tuple[PackComposition, ...] = COMPOSITION

    def register_builtin(self) -> list[RegisteredPack]:
        """Register every builtin manifest that is not registered yet."""
        known = set(self.packs.list_packs())
        discovered: list[RegisteredPack] = []
        for path in self.packs.builtin_manifest_paths():
            manifest = self._manifest_for(path)
            if manifest.package_id in known:
                continue
            discovered.append(self._register_manifest(manifest, path, anchor="builtin"))
        return discovered

    def activate_all(self) -> list[RegisteredPack]:
        """Register the builtins, then activate every pack the runtime knows.

        Returns the activated packs in composition order.  A pack with pending
        capabilities is *skipped*, not force-activated: the manifest contract
        ("a pack cannot register an operation by name alone") still holds, and
        :func:`composition_issues` is what reports the gap.
        """
        self.register_builtin()
        activated: list[RegisteredPack] = []
        for entry in self.composition:
            if entry.package_id not in self.packs:
                continue
            if self.packs.unknown_capabilities(entry.package_id):
                continue
            activated.append(self.packs.activate(entry.package_id))
        return activated

    def status(self) -> list[PackStatus]:
        """A row per builtin manifest, in composition order."""
        self.register_builtin()
        rows: list[PackStatus] = []
        by_id = {pack.package_id: pack for pack in self.packs.builtin_packs()}
        for entry in self.composition:
            pack = by_id.get(entry.package_id)
            if pack is None:  # pragma: no cover - guarded by composition_issues()
                continue
            rows.append(_status_for(entry, pack, self.packs))
        return rows

    def unknown_capabilities(self) -> dict[str, tuple[str, ...]]:
        """``package_id -> capabilities the runtime does not know`` (empty when complete)."""
        self.register_builtin()
        return {
            pack.package_id: self.packs.unknown_capabilities(pack.package_id)
            for pack in self.packs.builtin_packs()
            if self.packs.unknown_capabilities(pack.package_id)
        }

    @property
    def complete(self) -> bool:
        """The composition knows every operation of every builtin manifest."""
        return not self.unknown_capabilities()

    def operation_ids(self) -> tuple[str, ...]:
        return tuple(self.registry.list_operations())

    # -- internals ----------------------------------------------------------
    def _manifest_for(self, path: Path) -> CapabilityPackManifest:
        from nexus_ai_agent.creative.packs.manifest import load_manifest

        return load_manifest(path)

    def _register_manifest(
        self, manifest: CapabilityPackManifest, path: Path, *, anchor: Anchor
    ) -> RegisteredPack:
        return self.packs.register(manifest, source=str(path), anchor=anchor)


def _status_for(entry: PackComposition, pack: RegisteredPack, packs: PackRegistry) -> PackStatus:
    return PackStatus(
        package_id=pack.package_id,
        version=pack.manifest.version,
        directory=entry.directory,
        capabilities=tuple(pack.manifest.capabilities),
        pending=tuple(packs.unknown_capabilities(pack.package_id)),
        active=pack.active,
        signature_state=pack.report.signature_state,
        external_binaries=tuple(pack.manifest.external_binaries),
    )


def build_pack_runtime(
    *,
    current_version: str | None = None,
    tone_library: Any | None = None,
    compositions: Iterable[PackComposition] | None = None,
    register: bool = True,
    activate: bool = False,
) -> PackRuntime:
    """Build the Wave-5 runtime: composition first, registration on request.

    ``register=True`` (default) discovers the builtin manifests, so the returned
    runtime can be asked for a status table immediately.  ``activate=True`` also
    turns every complete pack on — used by tests and by operators who want a live
    runtime in one call; the CLI keeps activation explicit.
    """
    selected = tuple(compositions) if compositions is not None else COMPOSITION
    runtime = PackRuntime(
        registry=build_runtime_registry(tone_library=tone_library, compositions=selected),
        packs=build_pack_registry(
            current_version=current_version, tone_library=tone_library, compositions=selected
        ),
        composition=selected,
    )
    if register:
        runtime.register_builtin()
    if activate:
        runtime.activate_all()
    return runtime


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def composition_issues(root: Path | None = None) -> tuple[str, ...]:
    """Findings that make the composition *inconsistent* (empty tuple = clean).

    Three checks, each of which has bitten this repository before:

    1. every ``pack.manifest.json`` on disk has a :data:`COMPOSITION` entry
       (otherwise the pack is dead weight and ``nexus packs list`` shows it as
       pending forever);
    2. every :data:`COMPOSITION` entry has a manifest on disk (otherwise the
       builder is registered against nothing);
    3. the declared ``package_id`` in the composition matches the manifest's own
       ``package_id`` (otherwise activation would target the wrong id).
    """
    registry = PackRegistry(build_wave1_registry(), current_version=None)
    paths = registry.builtin_manifest_paths(root)
    on_disk: dict[str, CapabilityPackManifest] = {}
    for path in paths:
        from nexus_ai_agent.creative.packs.manifest import load_manifest

        manifest = load_manifest(path)
        on_disk[path.parent.name] = manifest

    issues: list[str] = []
    for directory, manifest in sorted(on_disk.items()):
        entry = COMPOSITION_BY_DIRECTORY.get(directory)
        if entry is None:
            issues.append(
                f"pack directory {directory!r} ships a manifest but has no composition entry"
            )
        elif entry.package_id != manifest.package_id:
            issues.append(
                f"pack directory {directory!r} declares {entry.package_id!r} "
                f"but its manifest says {manifest.package_id!r}"
            )
    for entry in COMPOSITION:
        if entry.directory not in on_disk:
            issues.append(
                f"composition entry {entry.label} has no pack.manifest.json under "
                f"{root or 'the builtin pack root'}"
            )
    return tuple(issues)


def stale_capabilities() -> dict[str, tuple[str, ...]]:
    """``package_id -> capabilities the composed runtime cannot execute``.

    This is the *activation-completeness* measurement: the architecture gate
    requires it to be empty, and its non-emptiness is precisely what Wave 5
    removed for all six builtin packs.
    """
    return build_pack_runtime().unknown_capabilities()


__all__ = [
    "COMPOSITION",
    "COMPOSITION_BY_DIRECTORY",
    "COMPOSITION_BY_PACKAGE_ID",
    "PackComposition",
    "PackRuntime",
    "PackRuntimeError",
    "PackStatus",
    "build_pack_registry",
    "build_pack_runtime",
    "build_runtime_registry",
    "composition_issues",
    "installed_version",
    "stale_capabilities",
]
