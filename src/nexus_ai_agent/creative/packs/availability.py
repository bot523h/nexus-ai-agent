"""Honest pack availability: registered is not the same as runnable.

Session 2 (task-176) closes the gap the Wave-5 report named: a pack can be
``active`` in the registry while its media engine is missing, and nothing told
the operator.  :func:`pack_availability` reports one of six states:

* ``REGISTERED`` — known to the runtime, not activated;
* ``AVAILABLE`` — activated and every probe passes;
* ``MISSING_DEPENDENCY`` — an activated pack whose declared Python dependency
  cannot be imported;
* ``MISSING_BINARY`` — an activated pack whose declared external binary cannot
  be resolved;
* ``DISABLED`` — operator-disabled (wins over every other state);
* ``FAILED`` — activation-impossible (pending capabilities) or a probe raised.

Two deliberate constraints keep this module inside the pack-substrate boundary
(``tests/architecture/test_pack_manifest_is_data_only.py`` forbids ``shutil`` /
``os`` / ``subprocess`` and any import outside ``creative.packs`` +
``creative.studio`` here):

* binary resolution is **injected** — the one canonical resolver already exists
  (``creative.slideshow.ffmpeg.resolve_ffmpeg_bin``) and this module must not
  grow a second ``PATH`` search that could shadow it.  Callers wire it in six
  lines (see ``docs/ops/CREATIVE_RUNTIME.md`` and the e2e test);
* Python-dependency probing uses only :mod:`importlib` (allow-listed):
  :func:`default_dependency_probe` answers ``find_spec`` without importing.

No pack today declares Python dependencies (the pure operations need none —
optional engines such as ``faster-whisper`` belong to the engine adapters, not
the pack contract), so ``MISSING_DEPENDENCY`` is reachable only through
explicitly declared ``python_dependencies``.  The only declared binary
repository-wide is ``ffmpeg`` (``nexus.slideshow.compose``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any


class Availability:
    """The six availability states (see the module docstring).

    A plain string namespace — not an :class:`enum.Enum`: the pack substrate
    allowlist (``tests/architecture/test_pack_manifest_is_data_only.py``)
    forbids the ``enum`` import here, and strings serialize without a mapping.
    """

    REGISTERED = "registered"
    AVAILABLE = "available"
    MISSING_DEPENDENCY = "missing_dependency"
    MISSING_BINARY = "missing_binary"
    DISABLED = "disabled"
    FAILED = "failed"


#: ``binary name -> resolved path`` (``None`` when unresolvable).
BinaryResolver = Callable[[str], str | None]

#: ``dotted module name -> importable``.
DependencyProbe = Callable[[str], bool]


@dataclass(frozen=True)
class BinaryProbe:
    """The outcome of resolving one declared external binary."""

    name: str
    found: bool
    path: str | None
    detail: str


@dataclass(frozen=True)
class DependencyProbeResult:
    """The outcome of probing one declared Python dependency."""

    name: str
    importable: bool
    detail: str


@dataclass(frozen=True)
class PackAvailability:
    """One row of ``PackRuntime.availability()``: state plus its evidence."""

    package_id: str
    availability: str
    active: bool
    pending_capabilities: tuple[str, ...]
    binaries: tuple[BinaryProbe, ...]
    dependencies: tuple[DependencyProbeResult, ...]
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "availability": self.availability,
            "active": self.active,
            "pending_capabilities": list(self.pending_capabilities),
            "binaries": [
                {"name": b.name, "found": b.found, "path": b.path, "detail": b.detail}
                for b in self.binaries
            ],
            "dependencies": [
                {"name": d.name, "importable": d.importable, "detail": d.detail}
                for d in self.dependencies
            ],
            "detail": self.detail,
        }


def default_dependency_probe(module_name: str) -> bool:
    """Answer whether ``module_name`` is importable without importing it."""
    try:
        return find_spec(module_name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def probe_binaries(
    names: tuple[str, ...], *, resolve_binary: BinaryResolver
) -> tuple[BinaryProbe, ...]:
    """Resolve every declared binary through the injected resolver."""
    probes: list[BinaryProbe] = []
    for name in names:
        try:
            path = resolve_binary(name)
        except Exception as exc:  # a broken resolver is evidence, not a crash
            probes.append(
                BinaryProbe(name=name, found=False, path=None, detail=f"resolver failed: {exc}")
            )
            continue
        if path:
            probes.append(BinaryProbe(name=name, found=True, path=path, detail="resolved"))
        else:
            probes.append(BinaryProbe(name=name, found=False, path=None, detail="unresolvable"))
    return tuple(probes)


def probe_dependencies(
    names: tuple[str, ...], *, check_dependency: DependencyProbe
) -> tuple[DependencyProbeResult, ...]:
    """Probe every declared Python dependency through the injected probe."""
    results: list[DependencyProbeResult] = []
    for name in names:
        try:
            importable = check_dependency(name)
        except Exception as exc:
            results.append(
                DependencyProbeResult(name=name, importable=False, detail=f"probe failed: {exc}")
            )
            continue
        results.append(
            DependencyProbeResult(
                name=name,
                importable=bool(importable),
                detail="importable" if importable else "not installed",
            )
        )
    return tuple(results)


def pack_availability(
    package_id: str,
    *,
    manifest: Any,
    active: bool,
    pending_capabilities: tuple[str, ...] = (),
    disabled: bool = False,
    resolve_binary: BinaryResolver,
    python_dependencies: tuple[str, ...] = (),
    check_dependency: DependencyProbe = default_dependency_probe,
) -> PackAvailability:
    """Compute the availability row for one pack (pure apart from the probes)."""
    if disabled:
        return PackAvailability(
            package_id=package_id,
            availability=Availability.DISABLED,
            active=active,
            pending_capabilities=tuple(pending_capabilities),
            binaries=(),
            dependencies=(),
            detail="operator-disabled",
        )
    if pending_capabilities:
        return PackAvailability(
            package_id=package_id,
            availability=Availability.FAILED,
            active=active,
            pending_capabilities=tuple(pending_capabilities),
            binaries=(),
            dependencies=(),
            detail=(
                "cannot activate: the runtime does not know " + ", ".join(pending_capabilities)
            ),
        )
    binaries = probe_binaries(tuple(manifest.external_binaries), resolve_binary=resolve_binary)
    dependencies = probe_dependencies(tuple(python_dependencies), check_dependency=check_dependency)
    if not active:
        return PackAvailability(
            package_id=package_id,
            availability=Availability.REGISTERED,
            active=False,
            pending_capabilities=(),
            binaries=binaries,
            dependencies=dependencies,
            detail="registered but not activated",
        )
    missing_binaries = [b.name for b in binaries if not b.found]
    if missing_binaries:
        failed = [b for b in binaries if "resolver failed" in b.detail]
        if failed:
            return PackAvailability(
                package_id=package_id,
                availability=Availability.FAILED,
                active=active,
                pending_capabilities=(),
                binaries=binaries,
                dependencies=dependencies,
                detail="binary resolver failed: " + ", ".join(b.name for b in failed),
            )
        return PackAvailability(
            package_id=package_id,
            availability=Availability.MISSING_BINARY,
            active=active,
            pending_capabilities=(),
            binaries=binaries,
            dependencies=dependencies,
            detail="missing binaries: " + ", ".join(missing_binaries),
        )
    missing_dependencies = [d.name for d in dependencies if not d.importable]
    if missing_dependencies:
        return PackAvailability(
            package_id=package_id,
            availability=Availability.MISSING_DEPENDENCY,
            active=active,
            pending_capabilities=(),
            binaries=binaries,
            dependencies=dependencies,
            detail="missing dependencies: " + ", ".join(missing_dependencies),
        )
    return PackAvailability(
        package_id=package_id,
        availability=Availability.AVAILABLE,
        active=active,
        pending_capabilities=(),
        binaries=binaries,
        dependencies=dependencies,
        detail="activated and every probe passes",
    )


__all__ = [
    "Availability",
    "BinaryProbe",
    "BinaryResolver",
    "DependencyProbe",
    "DependencyProbeResult",
    "PackAvailability",
    "default_dependency_probe",
    "pack_availability",
    "probe_binaries",
    "probe_dependencies",
]
