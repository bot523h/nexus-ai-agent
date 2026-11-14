"""F-006: the durability classification of every local store.

Arena finding F-006 asked for an audit of the SQLite-backed subsystems, and for
a machine-checkable answer to one question per store: *does production depend on
this state, and does that state actually survive a restart in the deployed
shape?* Prose in a runbook cannot answer that — a claim in a document drifts
from the code. This module is the single registry of the answer, so a test can
assert it and a deploy check can read it.

The classification vocabulary is fixed by the finding:

``DURABLE_AUTHORITATIVE``
    The store is a source of truth. Losing it loses user-visible state. It must
    live on a durable medium (PostgreSQL/Neon, or a mounted persistent volume).
``EPHEMERAL_CACHE``
    Rebuildable or disposable, but *shared* — losing it is a performance or
    freshness event, never data loss.
``PROCESS_LOCAL``
    Exists only inside one process lifetime (an in-memory map, a lock). Never
    the source of truth and never expected to outlive the process.
``DERIVED_REBUILDABLE``
    A projection of an authoritative store (an index, a vector table). It can be
    dropped and rebuilt from the authority.
``UNVERIFIED``
    Not yet classified. This is an honest terminal state, not a synonym for
    "probably fine": the check below treats it as a production risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Durability(str, Enum):
    """How a store's contents relate to durable, authoritative state."""

    DURABLE_AUTHORITATIVE = "DURABLE_AUTHORITATIVE"
    EPHEMERAL_CACHE = "EPHEMERAL_CACHE"
    PROCESS_LOCAL = "PROCESS_LOCAL"
    DERIVED_REBUILDABLE = "DERIVED_REBUILDABLE"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class StoreAudit:
    """One row of the F-006 audit.

    ``survives_scale_to_zero`` answers the deployment question directly: in a
    web/scale-to-zero shape with **no mounted volume**, does the bytes' home
    still exist after the instance stops? A ``None`` means "depends on the
    deployment shape" and is only allowed for stores that are not authoritative
    (a rebuildable cache may legitimately vanish).
    """

    name: str
    owner: str
    classification: Durability
    path_hint: str
    production_depends: bool
    survives_scale_to_zero: bool | None
    external_authority: str | None
    rationale: str


#: The audited stores. Keep this list aligned with
#: ``docs/architecture/DATA_AND_STORAGE.md`` §1 (the store map) — the test
#: ``tests/unit/test_store_durability_audit.py`` cross-checks both directions.
STORE_AUDIT: tuple[StoreAudit, ...] = (
    StoreAudit(
        name="Application DB (users/chats/messages/memory/…)",
        owner="storage/models.py + storage/db.py",
        classification=Durability.DURABLE_AUTHORITATIVE,
        path_hint="NEXUS_DATABASE_URL (PostgreSQL/Neon) — SQLite only in dev",
        production_depends=True,
        survives_scale_to_zero=True,
        external_authority="PostgreSQL / Neon",
        rationale=(
            "The product's source of truth. NEXUS_REQUIRE_POSTGRES=true makes "
            "the runtime refuse the SQLite fallback in production, so this "
            "store is on a durable external medium by construction (F-005)."
        ),
    ),
    StoreAudit(
        name="Job queue (nexus_job_queue)",
        owner="adapters/in_process_job_queue.py",
        classification=Durability.DURABLE_AUTHORITATIVE,
        path_hint="<db>.jobs.sqlite3 SQLite sidecar",
        production_depends=True,
        survives_scale_to_zero=False,
        external_authority=None,
        rationale=(
            "Holds pending/processing job rows — losing them loses accepted "
            "work. It is an authoritative SQLite sidecar with NO PostgreSQL "
            "backend and no Alembic revision, and the deployed Koyeb service "
            "mounts no volume, so on a scale-to-zero redeploy these rows are "
            "lost. This is the precise, unclosed durability gap F-006 names."
        ),
    ),
    StoreAudit(
        name="Causal journal (append-only hash chain)",
        owner="provenance/journal.py",
        classification=Durability.DURABLE_AUTHORITATIVE,
        path_hint="<db>.jobs.sqlite3.causal.sqlite3 SQLite sidecar",
        production_depends=True,
        survives_scale_to_zero=False,
        external_authority=None,
        rationale=(
            "The evidence chain for job transitions. Append-only and "
            "hash-chained, but still a local sidecar with no external "
            "authority; the same no-volume scale-to-zero gap applies."
        ),
    ),
    StoreAudit(
        name="Self-ledger (append-only hash chain)",
        owner="selfhood/ledger.py",
        classification=Durability.DURABLE_AUTHORITATIVE,
        path_hint="self-ledger SQLite sidecar",
        production_depends=False,
        survives_scale_to_zero=False,
        external_authority=None,
        rationale=(
            "Append-only self-state with the same hash-chain discipline. It is "
            "authoritative *for itself* but not on any production-critical "
            "request path, so it is not counted as a production dependency "
            "here; if it becomes one, it inherits the same gap."
        ),
    ),
    StoreAudit(
        name="Effect outbox (nexus_effect_outbox)",
        owner="adapters/outbox_dispatcher.py",
        classification=Durability.DURABLE_AUTHORITATIVE,
        path_hint="SQLite sidecar (own DDL)",
        production_depends=True,
        survives_scale_to_zero=False,
        external_authority=None,
        rationale=(
            "Delivery intent + lease + outcome: losing it loses the intent to "
            "deliver an effect. Same local-sidecar durability gap."
        ),
    ),
    StoreAudit(
        name="Checkpoints (LangGraph runtime state)",
        owner="storage/langgraph_checkpoint.py",
        classification=Durability.DERIVED_REBUILDABLE,
        path_hint="PostgreSQL (when NEXUS_DATABASE_URL) or SQLite sidecar",
        production_depends=False,
        survives_scale_to_zero=None,
        external_authority="PostgreSQL (production path)",
        rationale=(
            "Used only to resume a workflow; message history is the authority. "
            "A lost checkpoint costs a re-run, not data."
        ),
    ),
    StoreAudit(
        name="Checkpoint lifecycle index",
        owner="storage/checkpoint_lifecycle*.py",
        classification=Durability.DERIVED_REBUILDABLE,
        path_hint="nexus_checkpoint_lifecycle (PG) / SQLite sidecar",
        production_depends=False,
        survives_scale_to_zero=None,
        external_authority="PostgreSQL (production path)",
        rationale="Metadata about checkpoints (age, lineage, lock state).",
    ),
    StoreAudit(
        name="Vector store (LongTermMemory)",
        owner="memory/long_term.py",
        classification=Durability.DERIVED_REBUILDABLE,
        path_hint="settings.vector_path (data/vector.sqlite)",
        production_depends=False,
        survives_scale_to_zero=None,
        external_authority="Re-ingested from the application DB",
        rationale=(
            "An embedding index derived from stored content; safe to drop and "
            "re-ingest. Never the authority for the content itself."
        ),
    ),
    StoreAudit(
        name="Creative job registry",
        owner="creative/job_registry.py",
        classification=Durability.EPHEMERAL_CACHE,
        path_hint="settings.creative_temp_dir / creative_jobs.sqlite3",
        production_depends=False,
        survives_scale_to_zero=False,
        external_authority=None,
        rationale=(
            "Tracks in-flight creative jobs for status reporting. The store "
            "lives under the creative temp directory (which the store map "
            "already classes as ephemeral), so a redeploy clears it; that is "
            "acceptable only while the authoritative project state is "
            "elsewhere."
        ),
    ),
)


def classification_of(name: str) -> Durability:
    """Return the classification for a store by name, or ``UNVERIFIED``."""
    for row in STORE_AUDIT:
        if row.name == name:
            return row.classification
    return Durability.UNVERIFIED


def production_durability_risks(*, persistent_volume: bool) -> list[StoreAudit]:
    """Authoritative stores that a scale-to-zero deployment cannot keep.

    *persistent_volume* is the deployment fact the operator controls: a mounted
    durable volume makes local sidecars survivable, and the check then finds no
    risk. With no volume (the shipped ``koyeb.yaml``), every authoritative local
    sidecar is a risk — and ``UNVERIFIED`` stores are reported too, because an
    unclassified store must never be assumed safe.
    """
    risks: list[StoreAudit] = []
    for row in STORE_AUDIT:
        if row.classification is Durability.UNVERIFIED:
            risks.append(row)
            continue
        if row.classification is not Durability.DURABLE_AUTHORITATIVE:
            continue
        if not row.production_depends:
            continue
        if row.survives_scale_to_zero is False and not persistent_volume:
            risks.append(row)
    return risks
