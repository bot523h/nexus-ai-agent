"""Pack registry: registration, allow-listing and activation.

The registry is the seam between *declared* pack capabilities and the
runtime's :class:`~nexus_ai_agent.creative.studio.capabilities.CapabilityRegistry`:

* ``register`` validates a manifest and verifies it against the runtime's known
  operations.  An **external** pack with an unknown capability is rejected
  outright; a **builtin** pack (whose operation specs ship with this repository)
  may register with *pending* capabilities.
* ``activate`` refuses to activate a pack while any of its capabilities is still
  pending — an operation the runtime does not know can never be invoked, which
  is the TDD rule “a pack cannot register an operation by name alone”.

Wave 2a registers the eighth pack (``nexus.slideshow.compose``) as a builtin
manifest; its six operations are added to the runtime registry in Wave 2b, at
which point activation succeeds without changing this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nexus_ai_agent.creative.packs.manifest import (
    CapabilityPackManifest,
    PackManifestError,
    load_manifest,
)
from nexus_ai_agent.creative.packs.verify import (
    TRUSTED_SIGNATURE_STATES,
    VerificationReport,
    verify_manifest,
)
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry

Anchor = Literal["builtin", "external"]

#: Manifests that ship with the runtime live at ``<pack>/pack.manifest.json``.
BUILTIN_MANIFEST_GLOB = "*/pack.manifest.json"


class PackRegistryError(PackManifestError):
    """A pack could not be registered or activated."""


@dataclass(frozen=True)
class RegisteredPack:
    """A verified pack, plus where it came from and whether it is active."""

    manifest: CapabilityPackManifest
    report: VerificationReport
    source: str
    anchor: Anchor
    active: bool = False

    @property
    def package_id(self) -> str:
        return self.manifest.package_id

    @property
    def pending_capabilities(self) -> tuple[str, ...]:
        """Capabilities unknown to the runtime *at registration time* (snapshot).

        Activation never trusts this snapshot: it re-checks the live runtime, so
        a pack registered while its operations were still pending (Wave 2a)
        becomes activatable the moment the runtime learns them (Wave 2b).
        """
        return self.report.pending_capabilities


class PackRegistry:
    """Validated packs over one runtime capability registry."""

    def __init__(self, runtime_registry: CapabilityRegistry, *, current_version: str | None = None):
        self._runtime = runtime_registry
        self._current_version = current_version
        self._packs: dict[str, RegisteredPack] = {}

    # -- introspection ------------------------------------------------------
    @property
    def runtime_registry(self) -> CapabilityRegistry:
        return self._runtime

    @property
    def current_version(self) -> str | None:
        return self._current_version

    def list_packs(self) -> list[str]:
        return sorted(self._packs)

    def active_packs(self) -> list[str]:
        return sorted(pack_id for pack_id, pack in self._packs.items() if pack.active)

    def builtin_packs(self) -> list[RegisteredPack]:
        """Registered packs that ship with this repository, sorted by id.

        Wave 5 addition: the composition layer needs to talk about *builtin*
        packs as a group (status table, activation sweep, completeness gate)
        without re-globbing the filesystem or re-deriving ``anchor``.
        """
        return sorted(
            (pack for pack in self._packs.values() if pack.anchor == "builtin"),
            key=lambda pack: pack.package_id,
        )

    def get(self, package_id: str) -> RegisteredPack:
        try:
            return self._packs[package_id]
        except KeyError:
            raise PackRegistryError(f"pack is not registered: {package_id!r}") from None

    def operations(self, package_id: str) -> list[str]:
        return list(self.get(package_id).manifest.capabilities)

    def __contains__(self, package_id: object) -> bool:
        return package_id in self._packs

    # -- registration -------------------------------------------------------
    def register(
        self,
        manifest: CapabilityPackManifest,
        *,
        source: str = "<memory>",
        anchor: Anchor = "external",
        activate: bool = False,
    ) -> RegisteredPack:
        """Verify and register a manifest; raises if verification fails."""
        report = verify_manifest(
            manifest,
            known_operations=self._runtime.list_operations(),
            current_version=self._current_version,
            anchor=anchor,
        )
        report.raise_for_errors()
        if manifest.package_id in self._packs:
            raise PackRegistryError(f"pack is already registered: {manifest.package_id!r}")
        pack = RegisteredPack(
            manifest=manifest, report=report, source=source, anchor=anchor, active=False
        )
        self._packs[manifest.package_id] = pack
        if activate:
            self.activate(manifest.package_id)
        return self._packs[manifest.package_id]

    def activate(self, package_id: str) -> RegisteredPack:
        """Activate a registered pack once the runtime knows all its operations.

        The check is evaluated against the **live** runtime registry rather than
        the register-time report: a pack registered while its operations were
        still pending (Wave 2a) becomes activatable the moment the runtime
        learns them (Wave 2b), with no re-registration.
        """
        pack = self.get(package_id)
        # PACK-SEC-001: external manifests are an untrusted supply-chain
        # boundary.  The verifier can only emit ``placeholder`` or
        # ``format_only_unverified`` (Ed25519 verification is not wired in), and
        # ``TRUSTED_SIGNATURE_STATES`` is empty by construction, so activating an
        # external pack would turn a warning into executable trust — refused.
        # Builtins are repository-controlled and keep the existing activation
        # path until a real signature provider is introduced.
        if (
            pack.anchor == "external"
            and pack.report.signature_state not in TRUSTED_SIGNATURE_STATES
        ):
            raise PackRegistryError(
                f"{package_id}: cannot activate — external pack signature is not verified "
                f"(state {pack.report.signature_state!r}; no cryptographic verifier is wired in)"
            )
        unknown = tuple(
            capability
            for capability in pack.manifest.capabilities
            if capability not in self._runtime
        )
        if unknown:  # re-evaluated against the live registry, never the snapshot
            raise PackRegistryError(
                f"{package_id}: cannot activate — the runtime does not know " + ", ".join(unknown)
            )
        activated = RegisteredPack(
            manifest=pack.manifest,
            report=pack.report,
            source=pack.source,
            anchor=pack.anchor,
            active=True,
        )
        self._packs[package_id] = activated
        return activated

    def unknown_capabilities(self, package_id: str) -> tuple[str, ...]:
        """Capabilities the runtime still does not know (live, not a snapshot)."""
        pack = self.get(package_id)
        return tuple(
            capability
            for capability in pack.manifest.capabilities
            if capability not in self._runtime
        )

    def deactivate(self, package_id: str) -> RegisteredPack:
        pack = self.get(package_id)
        deactivated = RegisteredPack(
            manifest=pack.manifest,
            report=pack.report,
            source=pack.source,
            anchor=pack.anchor,
            active=False,
        )
        self._packs[package_id] = deactivated
        return deactivated

    # -- builtin discovery --------------------------------------------------
    @staticmethod
    def builtin_manifest_paths(root: Path | None = None) -> list[Path]:
        """Manifests that ship inside this package, sorted by path."""
        base = root if root is not None else Path(__file__).parent
        return sorted(base.glob(BUILTIN_MANIFEST_GLOB))

    def register_builtin(
        self, *, root: Path | None = None, activate: bool = False
    ) -> list[RegisteredPack]:
        """Register every builtin manifest found next to this module."""
        registered: list[RegisteredPack] = []
        for path in self.builtin_manifest_paths(root):
            manifest = load_manifest(path)
            registered.append(
                self.register(
                    manifest,
                    source=str(path),
                    anchor="builtin",
                    activate=activate,
                )
            )
        return registered
