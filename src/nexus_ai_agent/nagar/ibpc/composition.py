"""Composition-root factories for the IBPC service and approval key ring.

Alongside :func:`build_ibpc_service` (approvals, sessions, privacy) this module
owns :func:`build_unified_studio_runtime`, which is the one place the five
Wave-1..4 Studio modules are assembled into a running deployment:

* :class:`~nexus_ai_agent.adapters.studio_principal_auth.StudioPrincipalAuthStore`
  and the authority bridge over it (who may do what);
* :class:`~nexus_ai_agent.creative.studio.revision_pinning.DurableRevisionPinningStore`
  (which bytes and which plan a revision actually was);
* :class:`~nexus_ai_agent.creative.studio.preview_readback.StudioPreviewReadbackBuilder`
  and the WYSIWYS verification that closes the readback loop;
* :class:`~nexus_ai_agent.creative.studio.production_dr.StudioDisasterRecoveryManager`
  and its resource governor (what happens when the disk or the box fails);
* :class:`~nexus_ai_agent.nagar.ibpc.execution_outbox.ExecutionOutboxCoordinator`
  (what happens when the *process* fails between approval and enqueue).

Everything is optional and lazily constructed.  A deployment that wants only the
legacy approval path keeps calling :func:`build_ibpc_service` and never touches
this module, so wiring Waves 1-4 cannot break an existing caller.  A deployment
that wants the full loop calls this once and passes the runtime down.

Secrets are never invented here.  A disaster-recovery key is either supplied by
the caller or read from ``NEXUS_STUDIO_BACKUP_KEY_HEX``; when neither exists the
DR manager is simply absent from the runtime (``dr=None``) rather than being
handed a random key that would silently invalidate every existing backup.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from nexus_ai_agent.nagar.ibpc.approval import (
    DurableApprovalLedger,
    durable_approval_ledger_path,
)
from nexus_ai_agent.nagar.ibpc.approval_mac import (
    KeyProvider,
    KeyProviderError,
    StaticKeyRing,
)
from nexus_ai_agent.nagar.ibpc.privacy import (
    DurablePrivacyConsentStore,
    durable_privacy_consent_path,
)
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps
from nexus_ai_agent.nagar.ibpc.session import (
    DurableLiveSessionStore,
    durable_session_store_path,
)

#: Environment variable naming the disaster-recovery HMAC key (hex).
BACKUP_KEY_ENV = "NEXUS_STUDIO_BACKUP_KEY_HEX"
#: Environment variable naming the DR key id (never secret).
BACKUP_KEY_ID_ENV = "NEXUS_STUDIO_BACKUP_KEY_ID"
#: Environment variable naming the backup bundle directory.
BACKUP_ROOT_ENV = "NEXUS_STUDIO_BACKUP_ROOT"


def create_default_key_provider(
    environ: Mapping[str, str] | None = None,
) -> KeyProvider:
    """Load the configured active signing key and verification key ring.

    Missing or malformed production configuration is a hard startup failure;
    this factory deliberately has no development/test fallback secret.
    """
    return StaticKeyRing.from_environment(os.environ if environ is None else environ)


def build_ibpc_service(
    deps: IBPCServiceDeps,
    *,
    key_provider: KeyProvider | None = None,
    environ: Mapping[str, str] | None = None,
) -> IBPCService:
    """Build IBPC with an explicit, rotation-capable approval key provider and durable stores."""
    provider = key_provider if key_provider is not None else create_default_key_provider(environ)
    try:
        active_key_id = provider.active_key_id
        provider.key(active_key_id)
    except (AttributeError, KeyProviderError) as exc:
        raise KeyProviderError("approval key provider is not configured correctly") from exc
    updated = replace(deps, approval_key_provider=provider, durable=True)
    if updated.authorized_asset_root is None:
        updated = replace(updated, authorized_asset_root=updated.workspace_root)
    if updated.approval_ledger is None:
        updated = replace(
            updated,
            approval_ledger=DurableApprovalLedger(
                durable_approval_ledger_path(updated.workspace_root), now=updated.now
            ),
        )
    if updated.session_store is None:
        updated = replace(
            updated,
            session_store=DurableLiveSessionStore(
                durable_session_store_path(updated.workspace_root)
            ),
        )
    if updated.privacy_store is None:
        updated = replace(
            updated,
            privacy_store=DurablePrivacyConsentStore(
                durable_privacy_consent_path(updated.workspace_root)
            ),
        )
    return IBPCService(updated)


# --------------------------------------------------------------------------- #
# Wave 4: the unified runtime
# --------------------------------------------------------------------------- #
#: Filenames the runtime creates under the workspace root.  Named here so a
#: deployment can find, back up and restore every durable store from one list.
RUNTIME_STORE_FILENAMES: Mapping[str, str] = {
    "authority": "studio_authority.sqlite3",
    "revision_pins": "studio_revision_pins.sqlite3",
    "execution_outbox": "studio_execution_outbox.sqlite3",
}
BACKUP_DIRNAME = "studio_backups"


@dataclass(frozen=True)
class UnifiedStudioRuntime:
    """The assembled Studio deployment: every Wave-1..4 module, or ``None``.

    The fields are deliberately plain attributes rather than properties: this is
    a bag of collaborators handed to callers, not a service with behaviour, and
    a caller that wants one piece (say, just the pinning store) can take it
    without paying for the rest.

    ``None`` always means "this deployment did not configure that capability",
    never "it failed to build".  A capability that is configured and then fails
    to construct raises, because a half-wired deployment is worse than none.
    """

    workspace_root: Path
    authorized_asset_root: Path
    service: IBPCService
    authority: Any | None = None
    authority_bridge: Any | None = None
    revision_pins: Any | None = None
    execution_outbox: Any | None = None
    dr: Any | None = None
    governor: Any | None = None
    #: One entry per capability this deployment left off, with the reason.  An
    #: operator reads this instead of inferring "wired=False" from ``None``.
    notes: tuple[Mapping[str, Any], ...] = field(default=())
    owned_closables: tuple[Any, ...] = field(default=())

    # -- the readback/revision surface the API exposes ---------------------- #
    def preview_readback_for(self, **kwargs: Any) -> Any:
        """Build a ``StructuredPreviewReadback`` from the real builder.

        A thin, explicit seam: the API supplies the identity and the operations,
        and this returns the verified envelope.  It exists so the HTTP layer
        never has to know the builder's constructor signature.
        """
        from nexus_ai_agent.creative.studio.preview_readback import (
            StudioPreviewReadbackBuilder,
        )

        operations = kwargs.pop("operations", ())
        builder = StudioPreviewReadbackBuilder(**kwargs)
        for operation in operations:
            builder.add_operation(**operation)
        return builder.build_readback()

    def verify_readback(self, readback: Any, *, expected_digest: str = "") -> bool:
        from nexus_ai_agent.creative.studio.preview_readback import verify_wysiwys_readback

        return verify_wysiwys_readback(readback, expected_digest=expected_digest)

    def revision_status(self, project_id: str) -> dict[str, Any]:
        """The project's pinned head and revision list, read-only.

        Reports ``wired=False`` when no pinning store exists, so an operator can
        tell "nothing is pinned" from "pinning is not enabled" -- a distinction
        that matters when the answer decides whether a turn may execute.
        """
        if self.revision_pins is None:
            return {"project_id": project_id, "wired": False, "head": None, "revisions": []}
        revisions = self.revision_pins.list_revisions(project_id)
        return {
            "project_id": project_id,
            "wired": True,
            "head": self.revision_pins.head_of(project_id),
            "revisions": [
                {
                    "revision_id": envelope.revision_id,
                    "revision_number": envelope.revision_number,
                    "parent_revision_id": envelope.parent_revision_id,
                    "revision_jcs_sha256": envelope.revision_jcs_sha256,
                    "created_at_utc": envelope.created_at_utc,
                    "asset_pins": [
                        {
                            "asset_id": pin.asset_id,
                            "relative_path": pin.relative_path,
                            "content_sha256": pin.content_sha256,
                            "byte_size": pin.byte_size,
                        }
                        for pin in envelope.input_asset_pins
                    ],
                }
                for envelope in revisions
            ],
        }

    def dr_status(self) -> dict[str, Any]:
        """What disaster recovery is configured to do, without doing any of it."""
        if self.dr is None:
            return {"wired": False, "backup_root": None, "key_id": None, "bundles": []}
        root = Path(self.dr.backup_root)
        bundles = (
            sorted(path.name for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
        )
        return {
            "wired": True,
            "backup_root": str(root),
            "key_id": self.dr.key_id,
            "bundles": bundles,
        }

    def verify_backup(self, bundle: str) -> Any:
        """Verify one bundle by name; a bare name can never escape the root."""
        if self.dr is None:
            raise RuntimeError("disaster recovery is not configured")
        root = Path(self.dr.backup_root).resolve()
        if not bundle or "/" in bundle or "\\" in bundle or bundle in (".", ".."):
            raise ValueError(f"invalid bundle name: {bundle!r}")
        candidate = (root / bundle).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"bundle escapes the backup root: {bundle!r}")
        return self.dr.verify_backup_bundle(candidate)

    def close(self) -> None:
        """Close every owned store once, in dependency order (outbox last)."""
        # ``service.close()`` already closes the stores it owns; closing the same
        # SQLite handle twice raises, so each resource is closed at most once.
        seen: set[int] = set()
        for resource in self.owned_closables:
            if id(resource) in seen:
                continue
            seen.add(id(resource))
            close_fn = getattr(resource, "close", None)
            if callable(close_fn):
                close_fn()


def build_unified_studio_runtime(
    deps: IBPCServiceDeps,
    *,
    key_provider: KeyProvider | None = None,
    environ: Mapping[str, str] | None = None,
    store_filenames: Mapping[str, str] | None = None,
    backup_key_hex: str | None = None,
    backup_key_id: str | None = None,
    backup_root: str | Path | None = None,
    max_concurrent_jobs: int = 4,
    max_queue_depth: int = 64,
    enable_execution_outbox: bool = False,
    enable_disaster_recovery: bool = False,
    report: Any | None = None,
) -> UnifiedStudioRuntime:
    """Assemble the Studio runtime with every Wave-1..4 module wired in.

    Parameters are all optional so this factory is usable from three very
    different callers:

    * a production deployment enables everything and reads secrets from the
      environment;
    * a test passes explicit paths and a fixed clock;
    * a migration that has only pinned revisions so far enables one capability.

    The ``report`` argument is an optional collector (a list works) that receives
    one dict per disabled capability, so a caller can log *why* something is off
    instead of discovering it later.
    """
    env = os.environ if environ is None else environ
    names = store_filenames if store_filenames is not None else RUNTIME_STORE_FILENAMES
    root = Path(deps.workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    asset_root = Path(deps.authorized_asset_root or root)
    notes: list[dict[str, Any]] = []

    def _note(capability: str, reason: str) -> None:
        entry = {"capability": capability, "enabled": False, "reason": reason}
        notes.append(entry)
        if report is not None:
            report.append(entry)

    # -- pinning ----------------------------------------------------------- #
    from nexus_ai_agent.creative.studio.revision_pinning import DurableRevisionPinningStore

    revision_pins = DurableRevisionPinningStore(root / names["revision_pins"], now=deps.now)

    # -- execution outbox -------------------------------------------------- #
    execution_outbox = None
    if enable_execution_outbox:
        from nexus_ai_agent.nagar.ibpc.execution_outbox import DurableExecutionOutboxStore

        execution_outbox = DurableExecutionOutboxStore(
            root / names["execution_outbox"], now=deps.now
        )
    else:
        _note("execution_outbox", "disabled by the caller")

    # -- disaster recovery -------------------------------------------------- #
    dr = None
    governor = None
    if enable_disaster_recovery:
        from nexus_ai_agent.creative.studio.production_dr import (
            BackupKeyMaterial,
            StudioDisasterRecoveryManager,
            StudioResourceGovernor,
        )

        secret = (
            backup_key_hex if backup_key_hex is not None else env.get(BACKUP_KEY_ENV, "")
        ).strip()
        key_id = (
            backup_key_id if backup_key_id is not None else env.get(BACKUP_KEY_ID_ENV, "")
        ).strip() or "bk-studio"
        configured_root = backup_root if backup_root is not None else env.get(BACKUP_ROOT_ENV, "")
        if not secret:
            # Never invent a key: a random one would verify nothing that already
            # exists, so the manager is absent rather than misleading.
            _note("disaster_recovery", f"{BACKUP_KEY_ENV} is not configured")
        else:
            dr = StudioDisasterRecoveryManager(
                Path(configured_root) if configured_root else root / BACKUP_DIRNAME,
                key_material=BackupKeyMaterial(key_id, secret),
                now=deps.now,
            )
            governor = StudioResourceGovernor(
                max_concurrent_jobs=max_concurrent_jobs, max_queue_depth=max_queue_depth
            )
    else:
        _note("disaster_recovery", "disabled by the caller")

    # -- the authority bridge ---------------------------------------------- #
    # A bridge over the SAME studio auth store the deps already authorize
    # against: one durable principal/membership truth, read twice by two
    # different callers, beats two stores that could disagree.
    authority_bridge = None
    authority_store = getattr(deps, "authority", None)
    if authority_store is None:
        _note("authority_bridge", "deps carry no StudioPrincipalAuthStore")
    else:
        from nexus_ai_agent.adapters.studio_directory_bridge import (
            StudioUnifiedAuthorityBridge,
        )

        authority_bridge = StudioUnifiedAuthorityBridge(authority_store, now=deps.now)

    # -- the service itself ------------------------------------------------ #
    # ``execution_coordinator`` stays the raw store at this point: the real
    # ``ExecutionOutboxCoordinator`` is built by the SERVICE in
    # :meth:`IBPCService.rebuild_execution_coordinator`, because it must close
    # over the service's own consume/enqueue/verify callables.  Passing a store
    # here is what lets the service discover the outbox without a second
    # constructor argument.
    # The service ADOPTS the runtime's stores as owned closables, so the
    # existing per-request ``service.close()`` in every API route also releases
    # the pinning store and the outbox.  Without this the routes that call
    # ``close()`` on the service alone would leak one SQLite handle per request
    # -- a slow file-descriptor exhaustion rather than an obvious failure.
    owned_by_service: tuple[Any, ...] = (*deps.owned_closables, revision_pins)
    if execution_outbox is not None:
        owned_by_service = (*owned_by_service, execution_outbox)
    wired = replace(
        deps,
        revision_pinning_store=revision_pins,
        execution_coordinator=None,
        owned_closables=owned_by_service,
    )
    service = build_ibpc_service(wired, key_provider=key_provider, environ=environ)
    if execution_outbox is not None:
        # The coordinator is built *over* the live service: it needs to call this
        # service's consume/enqueue/verify steps, which do not exist before it.
        service.enable_execution_outbox(execution_outbox)

    owned: list[Any] = [service, revision_pins]
    if execution_outbox is not None:
        owned.append(execution_outbox)

    runtime = UnifiedStudioRuntime(
        workspace_root=root,
        authorized_asset_root=asset_root,
        service=service,
        authority=getattr(deps, "authority", None),
        authority_bridge=authority_bridge,
        revision_pins=revision_pins,
        execution_outbox=execution_outbox,
        dr=dr,
        governor=governor,
        notes=tuple(notes),
        owned_closables=tuple(owned),
    )
    return runtime


__all__ = [
    "BACKUP_DIRNAME",
    "BACKUP_KEY_ENV",
    "BACKUP_KEY_ID_ENV",
    "BACKUP_ROOT_ENV",
    "RUNTIME_STORE_FILENAMES",
    "UnifiedStudioRuntime",
    "build_ibpc_service",
    "build_unified_studio_runtime",
    "create_default_key_provider",
]
