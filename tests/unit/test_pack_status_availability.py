"""Pack availability: registered vs. actually runnable (task-176, session 2 P2).

``PackRuntime.status()`` answers the composition question; ``availability()``
answers the runtime question by probing declared external binaries through an
injected resolver.  Every state is pinned with a fake resolver (no PATH, no
network, no subprocess) plus one integration leg that wires the canonical
FFmpeg resolver.
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.availability import (
    Availability,
    default_dependency_probe,
    pack_availability,
)
from nexus_ai_agent.creative.packs.manifest import load_manifest
from nexus_ai_agent.creative.packs.runtime import (
    COMPOSITION_BY_PACKAGE_ID,
    build_pack_runtime,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)


def _slideshow_manifest():  # type: ignore[no-untyped-def]
    entry = COMPOSITION_BY_PACKAGE_ID["nexus.slideshow.compose"]
    from pathlib import Path

    from nexus_ai_agent.creative.packs import registry as registry_module

    root = Path(registry_module.__file__).parent
    return load_manifest(root / entry.directory / "pack.manifest.json")


def _resolve_present(name: str) -> str | None:
    return f"/usr/bin/{name}"


def _resolve_missing(name: str) -> str | None:
    return None


def test_registered_when_known_but_not_activated() -> None:
    row = pack_availability(
        "nexus.slideshow.compose",
        manifest=_slideshow_manifest(),
        active=False,
        resolve_binary=_resolve_present,
    )
    assert row.availability == Availability.REGISTERED
    assert row.active is False
    assert row.binaries and row.binaries[0].found is True


def test_available_when_activated_and_probes_pass() -> None:
    row = pack_availability(
        "nexus.slideshow.compose",
        manifest=_slideshow_manifest(),
        active=True,
        resolve_binary=_resolve_present,
    )
    assert row.availability == Availability.AVAILABLE
    assert "every probe passes" in row.detail


def test_missing_binary_when_ffmpeg_unresolvable() -> None:
    row = pack_availability(
        "nexus.slideshow.compose",
        manifest=_slideshow_manifest(),
        active=True,
        resolve_binary=_resolve_missing,
    )
    assert row.availability == Availability.MISSING_BINARY
    assert "ffmpeg" in row.detail
    assert row.binaries[0].found is False


def test_missing_dependency_when_declared_module_absent() -> None:
    row = pack_availability(
        "nexus.slideshow.compose",
        manifest=_slideshow_manifest(),
        active=True,
        resolve_binary=_resolve_present,
        python_dependencies=("definitely_not_installed_nagar_probe_zzz",),
    )
    assert row.availability == Availability.MISSING_DEPENDENCY
    assert "definitely_not_installed_nagar_probe_zzz" in row.detail


def test_disabled_wins_over_every_other_state() -> None:
    row = pack_availability(
        "nexus.slideshow.compose",
        manifest=_slideshow_manifest(),
        active=True,
        disabled=True,
        resolve_binary=_resolve_missing,
        python_dependencies=("definitely_not_installed_nagar_probe_zzz",),
    )
    assert row.availability == Availability.DISABLED


def test_failed_when_capabilities_are_pending() -> None:
    row = pack_availability(
        "nexus.slideshow.compose",
        manifest=_slideshow_manifest(),
        active=False,
        pending_capabilities=("slideshow.compose",),
        resolve_binary=_resolve_present,
    )
    assert row.availability == Availability.FAILED
    assert "slideshow.compose" in row.detail


def test_failed_when_the_resolver_itself_breaks() -> None:
    def _broken(name: str) -> str | None:
        raise RuntimeError("boom")

    row = pack_availability(
        "nexus.slideshow.compose",
        manifest=_slideshow_manifest(),
        active=True,
        resolve_binary=_broken,
    )
    assert row.availability == Availability.FAILED
    assert "resolver failed" in row.detail


def test_default_dependency_probe_never_imports() -> None:
    assert default_dependency_probe("json") is True
    assert default_dependency_probe("definitely_not_installed_nagar_probe_zzz") is False
    assert "definitely_not_installed_nagar_probe_zzz" not in __import__("sys").modules


def test_runtime_availability_covers_every_builtin_pack() -> None:
    runtime = build_pack_runtime(activate=True)
    rows = runtime.availability(resolve_binary=_resolve_present)
    assert [row.package_id for row in rows] == [
        entry.package_id for entry in runtime.composition
    ]
    assert all(row.availability == Availability.AVAILABLE for row in rows)
    missing = runtime.availability(resolve_binary=_resolve_missing)
    by_id = {row.package_id: row for row in missing}
    # Only the slideshow pack declares an external binary.
    assert by_id["nexus.slideshow.compose"].availability == Availability.MISSING_BINARY
    for package_id, row in by_id.items():
        if package_id != "nexus.slideshow.compose":
            assert row.availability == Availability.AVAILABLE, package_id


def test_runtime_availability_respects_disabled_and_activation() -> None:
    runtime = build_pack_runtime(activate=True)
    rows = runtime.availability(
        resolve_binary=_resolve_present, disabled=frozenset({"nexus.edit.timeline"})
    )
    by_id = {row.package_id: row for row in rows}
    assert by_id["nexus.edit.timeline"].availability == Availability.DISABLED

    idle = build_pack_runtime()
    idle_rows = idle.availability(resolve_binary=_resolve_present)
    assert all(row.availability == Availability.REGISTERED for row in idle_rows)


def test_canonical_ffmpeg_wiring_smoke() -> None:
    """The documented six-line production wiring resolves (or honestly misses)."""

    def _resolve(name: str) -> str | None:
        if name == "ffmpeg":
            try:
                return resolve_ffmpeg_bin()
            except FfmpegUnavailableError:
                return None
        import shutil

        return shutil.which(name)

    runtime = build_pack_runtime(activate=True)
    by_id = {row.package_id: row for row in runtime.availability(resolve_binary=_resolve)}
    slideshow = by_id["nexus.slideshow.compose"]
    try:
        expected_path = resolve_ffmpeg_bin()
    except FfmpegUnavailableError:
        assert slideshow.availability == Availability.MISSING_BINARY
    else:
        assert slideshow.availability == Availability.AVAILABLE
        assert slideshow.binaries[0].path == expected_path
    assert slideshow.as_dict()["package_id"] == "nexus.slideshow.compose"
