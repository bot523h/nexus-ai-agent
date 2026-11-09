"""Durable storage: content-addressed blobs, a SQLite capsule repository, lineage.

Design notes
------------
* **Content addressing, measured not declared.** The digest of a reference asset
  is computed from the bytes actually received. A caller's filename or MIME type
  never influences the recorded digest or the accepted type, and the filename is
  never used as a path.
* **Encryption is the default, plaintext is a recorded decision.** Constructing a
  repository without a cipher raises unless the caller passes
  ``allow_plaintext="<reason>"``; the reason is stored in the database and
  reported by :meth:`CapsuleRepository.storage_policy`.
* **Blob identity is the plaintext digest.** Identical bytes registered twice
  (even with a fresh data key each time) resolve to the same stored blob: the
  existing ciphertext is decrypted and compared before it is reused. Ownership is
  a database fact (``assets.character_id``) and is checked on every read.
* **Authorization and consent are checked before any protected byte is read.**
  Export requires ``Permission.EXPORT`` *and*, for a real person, a consent whose
  purpose, right, territory and validity window all match.
* **Export and import are atomic.** Export builds a staging directory and swaps it
  in at the end, so a failed export cannot destroy the previous destination.
  Import validates the whole bundle (containment, digests, capsule seal) before a
  single row is written, then writes in one transaction.
* **SQLite + filesystem only.** No queue, no message bus, no second registry.

The prototype deliberately uses ``sqlite3`` and ``pathlib`` rather than the
repository's SQLAlchemy models: it must stay runnable and auditable in isolation
from the Arena-1 runtime, and it must not create a second schema inside NEXUS's
database.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canon import CanonFact, CanonModule, FactKind, StoryEvent
from .canonical import SCOPE_LINEAGE, canonical_json, scoped_digest, sha256_hex
from .errors import (
    AccessDenied,
    AssetRejected,
    ConsentRequired,
    CryptoUnavailable,
    DuplicateIdentity,
    ExportBlocked,
    IntegrityError,
    MediaUnsupported,
    RevisionConflict,
    UnknownReference,
    ValidationError,
)
from .media import sniff_media_type
from .model import (
    AssetRef,
    BodyModule,
    CharacterCapsule,
    CharacterIdentity,
    FaceModule,
    GaitProfile,
    HairModule,
    MotionModule,
    SubjectClass,
    VoiceModule,
    next_character_id,
)
from .scene import Scene, ShotReference, Timeline, validate_shot_reference
from .security import (
    AccessPolicy,
    Cipher,
    ConsentDecision,
    ConsentLedger,
    ConsentPurpose,
    ConsentRecord,
    Permission,
    Principal,
    _utc_now,
)
from .wardrobe import Outfit, WardrobeChangeEvent, WardrobeModule

#: Bumped when stored payloads change shape. 2 adds consent revocation history
#: and the quarantine table for imported consent.
SCHEMA_VERSION = 2
DEFAULT_MAX_ASSET_BYTES = 16 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{0,128}$")
ALLOWED_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "audio/wav"})
BUNDLE_SCHEMA = "character-capsule-bundle/1"


# ---------------------------------------------------------------------------
# Content-addressed blob store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlobRecord:
    """What the store durably holds for one digest."""

    sha256: str
    size_bytes: int
    media_type: str
    encrypted: bool
    key_id: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "encrypted": self.encrypted,
            "key_id": self.key_id,
        }


class BlobStore:
    """Digest-named blob directory with authenticated encryption by default."""

    def __init__(
        self,
        root: str | Path,
        *,
        cipher: Cipher | None = None,
        allow_plaintext: str = "",
        max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if cipher is None:
            if not allow_plaintext.strip():
                # Import locally to avoid a circular import at module scope.
                from .security import PlaintextCipher

                raise CryptoUnavailable(
                    "refusing to store protected assets without a cipher; pass a cipher or an "
                    "explicit allow_plaintext='<reason>' for a scratch/lab store",
                    hint="PlaintextCipher(reason=...) exists for exactly this documented case",
                )
            from .security import PlaintextCipher

            cipher = PlaintextCipher(reason=allow_plaintext)
        self.cipher = cipher
        self.max_bytes = int(max_bytes)

    # -- paths ------------------------------------------------------------
    def path_for(self, digest: str) -> Path:
        hexdigest = digest.split(":", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{64}", hexdigest):
            raise AssetRejected("malformed digest", digest=digest)
        return self.root / hexdigest[:2] / hexdigest[2:4] / hexdigest

    @property
    def confidential(self) -> bool:
        return bool(getattr(self.cipher, "provides_confidentiality", True))

    @staticmethod
    def _header(*, purpose: str, digest: str) -> dict[str, Any]:
        # Binding is purpose + content. Ownership lives in the database row, not
        # in the crypto header: binding the owner would make identical bytes
        # un-dedupable and still not prove who may read them.
        return {"purpose": purpose, "sha256": digest}

    # -- write ------------------------------------------------------------
    def put(
        self,
        data: bytes,
        *,
        declared_name: str = "",
        purpose: str = "reference-asset",
    ) -> BlobRecord:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise AssetRejected("refusing to store empty or non-bytes content")
        if len(data) > self.max_bytes:
            raise AssetRejected(
                "asset exceeds the configured size cap",
                size=len(data),
                cap=self.max_bytes,
            )
        if declared_name and not SAFE_NAME_RE.match(declared_name):
            raise AssetRejected(
                "declared filename is not a safe display name (no paths, no traversal)",
                name=declared_name,
            )
        media_type = sniff_media_type(bytes(data))
        if media_type is None or media_type not in ALLOWED_MEDIA_TYPES:
            raise MediaUnsupported(
                "media type is outside the supported allowlist (png/jpeg/wav)",
                detected=media_type,
            )
        plaintext = bytes(data)
        digest = sha256_hex(plaintext)
        header = self._header(purpose=purpose, digest=digest)
        target = self.path_for(digest)
        if target.exists():
            # Identical bytes may be registered again (another role, another
            # character, another run). Prove the stored blob decrypts to these
            # exact bytes before reusing it; a mismatch means collision or tamper.
            try:
                existing = self.cipher.decrypt(target.read_bytes(), header=header)
            except Exception as exc:  # noqa: BLE001 - reuse is only safe when proven
                raise IntegrityError(
                    "a blob exists under this digest but cannot be read back with the current "
                    "cipher/context; refusing to overwrite it",
                    digest=digest,
                    reason=str(exc),
                ) from exc
            if existing != plaintext:
                raise IntegrityError(
                    "a different blob is already stored under this digest (collision or tamper)",
                    digest=digest,
                )
        else:
            payload = self.cipher.encrypt(plaintext, header=header)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_suffix(".staging")
            staging.write_bytes(payload)
            os.replace(staging, target)
        return BlobRecord(
            sha256=digest,
            size_bytes=len(plaintext),
            media_type=media_type,
            encrypted=self.confidential,
            key_id=self.cipher.key_id,
        )

    # -- read -------------------------------------------------------------
    def read(self, record: BlobRecord, *, purpose: str = "reference-asset") -> bytes:
        path = self.path_for(record.sha256)
        if not path.exists():
            raise UnknownReference("blob is missing from the store", digest=record.sha256)
        raw = path.read_bytes()
        if self.confidential:
            if not record.encrypted:
                raise IntegrityError(
                    "the store is encrypted but the record says plaintext: refusing a downgrade",
                    digest=record.sha256,
                )
            header = self._header(purpose=purpose, digest=record.sha256)
            data = self.cipher.decrypt(raw, header=header)
        else:
            data = raw
        if len(data) != record.size_bytes:
            raise IntegrityError(
                "blob size does not match the record",
                digest=record.sha256,
                recorded=record.size_bytes,
                actual=len(data),
            )
        if sha256_hex(data) != record.sha256:
            raise IntegrityError(
                "blob content digest does not match the record", digest=record.sha256
            )
        return data

    def verify(self, record: BlobRecord, *, purpose: str = "reference-asset") -> None:
        """Re-measure the stored bytes; raises on any mismatch."""
        self.read(record, purpose=purpose)


# ---------------------------------------------------------------------------
# Module (de)serialisation
# ---------------------------------------------------------------------------


def capsule_from_payload(payload: Mapping[str, Any]) -> CharacterCapsule:
    """Rebuild a capsule from its stored JSON payload (fails closed on schema drift)."""
    if payload.get("schema") != "character-capsule/1":
        raise ValidationError("unsupported capsule schema", schema=payload.get("schema"))
    modules_payload = payload.get("modules") or {}
    modules: dict[str, Any] = {}
    for name, data in modules_payload.items():
        if name == "face":
            modules[name] = FaceModule(**_filter_face(data))
        elif name == "body":
            modules[name] = BodyModule(**{k: v for k, v in data.items()})
        elif name == "hair":
            data = dict(data)
            data["consistency_rules"] = tuple(data.get("consistency_rules", ()))
            modules[name] = HairModule(**data)
        elif name == "voice":
            data = dict(data)
            data["approved_voice_refs"] = tuple(data.get("approved_voice_refs", ()))
            modules[name] = VoiceModule(**data)
        elif name == "motion":
            data = dict(data)
            gait = GaitProfile(**(data.get("gait") or {}))
            modules[name] = MotionModule(
                revision=int(data["revision"]),
                gait=gait,
                gesture_style=str(data.get("gesture_style", "restrained")),
                pose_constraints=tuple(data.get("pose_constraints", ())),
            )
        elif name == "wardrobe":
            modules[name] = wardrobe_from_payload(data)
        elif name == "canon":
            modules[name] = canon_from_payload(data)
        else:
            raise ValidationError("unknown module in stored capsule", module=name)
    capsule = CharacterCapsule(
        character_id=str(payload["character_id"]),
        identity_version=int(payload["identity_version"]),
        revision_no=int(payload["revision_no"]),
        modules=modules,
        asset_refs=tuple(AssetRef(**ref) for ref in payload.get("assets", [])),
        digest=str(payload.get("digest", "")),
        note=str(payload.get("note", "")),
    )
    capsule.verify()
    return capsule


def _filter_face(data: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "revision",
        "eye_colour",
        "eye_spacing_ratio",
        "eye_size_ratio",
        "jaw_width_ratio",
        "nose_length_ratio",
        "nose_width_ratio",
        "mouth_width_ratio",
        "brow_thickness_ratio",
        "skin_colour",
        "skin_texture",
        "freckles",
    }
    return {key: value for key, value in data.items() if key in allowed}


def wardrobe_from_payload(data: Mapping[str, Any]) -> WardrobeModule:
    outfits = {
        key: Outfit(
            **{
                **value,
                "accessories": tuple(value.get("accessories", ())),
                "supersedes": tuple(value.get("supersedes", ())),
            }
        )
        for key, value in (data.get("outfits") or {}).items()
    }
    events = tuple(WardrobeChangeEvent(**event) for event in data.get("change_events") or [])
    return WardrobeModule(
        revision=int(data["revision"]),
        outfits=outfits,
        sequence_baselines=data.get("sequence_baselines") or {},
        change_events=events,
        scene_rules=data.get("scene_rules") or {},
    )


def canon_from_payload(data: Mapping[str, Any]) -> CanonModule:
    facts = {
        key: CanonFact(
            fact_id=value["fact_id"],
            predicate=value["predicate"],
            value=value["value"],
            kind=FactKind(value.get("kind", "IMMUTABLE")),
            added_at_scene=value.get("added_at_scene"),
        )
        for key, value in (data.get("facts") or {}).items()
    }
    events = tuple(
        StoryEvent(
            event_id=event["event_id"],
            kind=event["kind"],
            scene_order=int(event["scene_order"]),
            description=event["description"],
            effects=event.get("effects") or {},
        )
        for event in data.get("story_events") or []
    )
    return CanonModule(
        revision=int(data["revision"]),
        facts=facts,
        story_events=events,
        biography=str(data.get("biography", "")),
        personality=tuple(data.get("personality", ())),
        relationships=data.get("relationships") or {},
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS characters (
    character_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_versions (
    character_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (character_id, version)
);
CREATE TABLE IF NOT EXISTS capsule_revisions (
    character_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (character_id, revision_no)
);
CREATE TABLE IF NOT EXISTS capsules (
    character_id TEXT PRIMARY KEY,
    revision_no INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    role TEXT NOT NULL,
    display_name TEXT NOT NULL,
    encrypted INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (character_id, sha256, role)
);
CREATE TABLE IF NOT EXISTS scenes (scene_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shots (
    shot_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
    consent_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_revocations (
    consent_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    revoked_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    record_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_imports (
    consent_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_bundle_digest TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    adopted INTEGER NOT NULL DEFAULT 0,
    adopted_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lineage (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    principal TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    detail TEXT NOT NULL
);
"""


class CapsuleRepository:
    """Durable, compare-and-swap capsule repository (SQLite + blob store)."""

    def __init__(
        self,
        root: str | Path,
        *,
        cipher: Cipher | None = None,
        allow_plaintext: str = "",
        max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
        now: Any = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "capsules.sqlite3"
        self.blobs = BlobStore(
            self.root / "blobs",
            cipher=cipher,
            allow_plaintext=allow_plaintext,
            max_bytes=max_asset_bytes,
        )
        self.consents = ConsentLedger(now=now)
        self.access = AccessPolicy()
        self._now = now or _utc_now
        self._allow_plaintext = allow_plaintext.strip()
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        if self._allow_plaintext:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('plaintext_reason', ?)",
                (self._allow_plaintext,),
            )
        self._conn.commit()
        self._load_consents()

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CapsuleRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def storage_policy(self) -> dict[str, Any]:
        """What this store promises about confidentiality — measured, not assumed."""
        row = self._conn.execute("SELECT value FROM meta WHERE key='plaintext_reason'").fetchone()
        return {
            "encrypted": self.blobs.confidential,
            "cipher": type(self.blobs.cipher).__name__,
            "key_id": self.blobs.cipher.key_id,
            "plaintext_reason": None if self.blobs.confidential else (row["value"] if row else ""),
            "max_asset_bytes": self.blobs.max_bytes,
            "schema_version": self.schema_version,
        }

    # -- consent rehydration ---------------------------------------------
    def _load_consents(self) -> None:
        """Restore consent history from disk (a restart must not forget a revocation)."""
        rows = self._conn.execute("SELECT payload FROM consents ORDER BY consent_id").fetchall()
        records = [ConsentRecord.from_dict(json.loads(row["payload"])) for row in rows]
        self.consents.hydrate(records)

    # -- characters -------------------------------------------------------
    def create_character(
        self,
        *,
        display_name: str,
        subject_class: Any,
        work_title: str,
        character_id: str | None = None,
        principals: Iterable[Principal] = (),
    ) -> CharacterIdentity:
        existing = [
            row["character_id"] for row in self._conn.execute("SELECT character_id FROM characters")
        ]
        chosen = character_id or next_character_id(existing)
        if chosen in set(existing):
            raise DuplicateIdentity(f"character id {chosen} already exists", character_id=chosen)
        identity = CharacterIdentity(
            character_id=chosen,
            display_name=display_name,
            subject_class=subject_class,
            work_title=work_title,
            created_at=self._now(),
        )
        for principal in principals:
            self.access.require(principal, Permission.CREATE_CAPSULE, character_id=chosen)
        self._conn.execute(
            "INSERT INTO characters(character_id, payload, created_at) VALUES (?,?,?)",
            (chosen, canonical_json(identity.payload()), identity.created_at),
        )
        self._append_lineage("character.created", identity.payload())
        self._conn.commit()
        return identity

    def identity(self, character_id: str) -> CharacterIdentity:
        row = self._conn.execute(
            "SELECT payload FROM characters WHERE character_id=?", (character_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference(f"unknown character {character_id!r}", character_id=character_id)
        data = json.loads(row["payload"])
        return CharacterIdentity(
            character_id=data["character_id"],
            display_name=data["display_name"],
            subject_class=SubjectClass(data["subject_class"]),
            work_title=data["work_title"],
            created_at=data["created_at"],
            policy_version=data.get("policy_version", "character-identity/1"),
        )

    def character_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                row["character_id"]
                for row in self._conn.execute("SELECT character_id FROM characters")
            )
        )

    def subject_class(self, character_id: str) -> SubjectClass:
        return self.identity(character_id).subject_class

    # -- capsules ---------------------------------------------------------
    def save_capsule(
        self,
        capsule: CharacterCapsule,
        *,
        expected_revision: int | None = None,
    ) -> CharacterCapsule:
        capsule.verify()
        row = self._conn.execute(
            "SELECT revision_no, digest FROM capsules WHERE character_id=?",
            (capsule.character_id,),
        ).fetchone()
        current_revision = int(row["revision_no"]) if row else 0
        if expected_revision is not None and int(expected_revision) != current_revision:
            raise RevisionConflict(
                "capsule changed since the caller read it",
                expected=expected_revision,
                actual=current_revision,
            )
        if capsule.identity_version > 1:
            iv = self._conn.execute(
                "SELECT MAX(version) AS v FROM identity_versions WHERE character_id=?",
                (capsule.character_id,),
            ).fetchone()
            if int(iv["v"] or 0) < capsule.identity_version:
                raise RevisionConflict(
                    "identity version was not recorded before the capsule revision",
                    character_id=capsule.character_id,
                    identity_version=capsule.identity_version,
                )
        if current_revision == 0:
            if capsule.revision_no != 1:
                raise RevisionConflict("a new capsule must start at revision 1")
        else:
            if capsule.revision_no != current_revision + 1:
                raise RevisionConflict(
                    "capsule revisions must advance by exactly one",
                    current=current_revision,
                    proposed=capsule.revision_no,
                )
            if capsule.digest == row["digest"]:
                raise RevisionConflict("revision content is identical to the stored revision")
        payload = canonical_json(capsule.to_dict())
        try:
            self._conn.execute(
                "INSERT INTO capsule_revisions("
                "character_id, revision_no, digest, payload, created_at) VALUES (?,?,?,?,?)",
                (capsule.character_id, capsule.revision_no, capsule.digest, payload, self._now()),
            )
            self._conn.execute(
                "INSERT INTO capsules(character_id, revision_no, digest, payload) "
                "VALUES (?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET "
                "revision_no=excluded.revision_no, digest=excluded.digest, "
                "payload=excluded.payload",
                (capsule.character_id, capsule.revision_no, capsule.digest, payload),
            )
            self._append_lineage("capsule.revision", capsule.payload())
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return capsule

    def load_capsule(self, character_id: str) -> CharacterCapsule:
        row = self._conn.execute(
            "SELECT payload FROM capsules WHERE character_id=?", (character_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference(
                f"no capsule stored for {character_id!r}", character_id=character_id
            )
        return capsule_from_payload(json.loads(row["payload"]))

    def revision_at(self, character_id: str, revision_no: int) -> CharacterCapsule:
        row = self._conn.execute(
            "SELECT payload FROM capsule_revisions WHERE character_id=? AND revision_no=?",
            (character_id, int(revision_no)),
        ).fetchone()
        if row is None:
            raise UnknownReference(
                "no such capsule revision", character_id=character_id, revision_no=revision_no
            )
        return capsule_from_payload(json.loads(row["payload"]))

    def revision_history(self, character_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            "SELECT revision_no, digest, created_at FROM capsule_revisions "
            "WHERE character_id=? ORDER BY revision_no",
            (character_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def save_identity_version(self, capsule: CharacterCapsule, *, note: str = "") -> str:
        body = {
            "character_id": capsule.character_id,
            "version": capsule.identity_version,
            "face": capsule.module("face").payload(),
            "body": capsule.module("body").payload(),
            "voice": capsule.module("voice").payload() if "voice" in capsule.modules else None,
            "note": note,
        }
        digest = scoped_digest("identity", body)
        try:
            self._conn.execute(
                "INSERT INTO identity_versions(character_id, version, digest, payload) "
                "VALUES (?,?,?,?)",
                (capsule.character_id, capsule.identity_version, digest, canonical_json(body)),
            )
            self._append_lineage(
                "identity.version", {"digest": digest, "version": capsule.identity_version}
            )
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return digest

    def identity_versions(self, character_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            "SELECT version, digest FROM identity_versions WHERE character_id=? ORDER BY version",
            (character_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    # -- assets -----------------------------------------------------------
    def register_asset(
        self,
        character_id: str,
        data: bytes,
        *,
        role: str,
        display_name: str = "",
        purpose: str = "reference-asset",
    ) -> AssetRef:
        self.identity(character_id)  # existence check before any write
        record = self.blobs.put(data, declared_name=display_name, purpose=purpose)
        row = self._conn.execute(
            "SELECT asset_id FROM assets WHERE character_id=? AND sha256=? AND role=?",
            (character_id, record.sha256, role),
        ).fetchone()
        if row is not None:
            # Same bytes, same role, same character: the asset already exists.
            return AssetRef(
                asset_id=row["asset_id"],
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                media_type=record.media_type,
                role=role,
            )
        asset_id = self._next_asset_id()
        try:
            self._conn.execute(
                "INSERT INTO assets(asset_id, character_id, sha256, size_bytes, media_type, role, "
                "display_name, encrypted, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    asset_id,
                    character_id,
                    record.sha256,
                    record.size_bytes,
                    record.media_type,
                    role,
                    display_name,
                    int(record.encrypted),
                    self._now(),
                ),
            )
            self._append_lineage(
                "asset.registered",
                {
                    "asset_id": asset_id,
                    "character_id": character_id,
                    "sha256": record.sha256,
                    "role": role,
                },
            )
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return AssetRef(
            asset_id=asset_id,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            media_type=record.media_type,
            role=role,
        )

    def _next_asset_id(self) -> str:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()
        return f"AST-{int(row['n']) + 1:06d}"

    def _asset_row(self, asset_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if row is None:
            raise UnknownReference(f"unknown asset {asset_id!r}", asset_id=asset_id)
        return row

    def asset_bytes(self, asset_id: str, *, character_id: str | None = None) -> bytes:
        """Read an asset's plaintext. Ownership is checked when an owner is given."""
        row = self._asset_row(asset_id)
        if character_id is not None and row["character_id"] != character_id:
            raise AccessDenied(
                "asset does not belong to this character",
                asset_id=asset_id,
                owner=row["character_id"],
                requested=character_id,
            )
        record = BlobRecord(
            sha256=row["sha256"],
            size_bytes=int(row["size_bytes"]),
            media_type=row["media_type"],
            encrypted=bool(row["encrypted"]),
        )
        return self.blobs.read(record, purpose="reference-asset")

    def asset_record(self, asset_id: str) -> dict[str, Any]:
        return dict(self._asset_row(asset_id))

    def verify_integrity(self, character_id: str) -> dict[str, Any]:
        """Re-measure every asset of a character; report, never silently pass."""
        capsule = self.load_capsule(character_id)
        checked: list[str] = []
        failures: list[dict[str, Any]] = []
        for ref in capsule.asset_refs:
            checked.append(ref.asset_id)
            try:
                data = self.asset_bytes(ref.asset_id, character_id=character_id)
            except Exception as exc:  # noqa: BLE001 - report the typed reason
                failures.append({"asset_id": ref.asset_id, "reason": str(exc)})
                continue
            if sha256_hex(data) != ref.sha256:
                failures.append(
                    {"asset_id": ref.asset_id, "reason": "digest mismatch", "expected": ref.sha256}
                )
        return {
            "character_id": character_id,
            "capsule_digest": capsule.digest,
            "checked": checked,
            "failures": failures,
            "ok": not failures,
        }

    # -- scenes and shots -------------------------------------------------
    def save_scene(self, scene: Scene) -> Scene:
        self._conn.execute(
            "INSERT INTO scenes(scene_id, payload) VALUES (?,?) ON CONFLICT(scene_id) DO UPDATE "
            "SET payload=excluded.payload",
            (scene.scene_id, canonical_json(scene.to_dict())),
        )
        self._conn.commit()
        return scene

    def timeline(self) -> Timeline:
        rows = self._conn.execute("SELECT payload FROM scenes ORDER BY scene_id").fetchall()
        scenes = []
        for row in rows:
            data = json.loads(row["payload"])
            scenes.append(
                Scene(
                    scene_id=data["scene_id"],
                    episode=int(data["episode"]),
                    sequence_id=data["sequence_id"],
                    order=int(data["order"]),
                    title=data["title"],
                    environment=_environment_from(data.get("environment")),
                )
            )
        if not scenes:
            raise UnknownReference("no scenes are stored")
        return Timeline(scenes)

    def save_shot(self, shot: ShotReference, *, capsule: CharacterCapsule) -> ShotReference:
        validate_shot_reference(shot, timeline=self.timeline(), capsule=capsule)
        self._conn.execute(
            "INSERT INTO shots(shot_id, character_id, scene_id, payload) VALUES (?,?,?,?) "
            "ON CONFLICT(shot_id) DO UPDATE SET payload=excluded.payload, "
            "character_id=excluded.character_id, scene_id=excluded.scene_id",
            (shot.shot_id, shot.character_id, shot.scene_id, canonical_json(shot.to_dict())),
        )
        self._append_lineage("shot.authored", shot.to_dict())
        self._conn.commit()
        return shot

    def shots(self, *, character_id: str | None = None) -> tuple[ShotReference, ...]:
        # The only dynamic SQL in this module: a fixed literal plus a bound
        # parameter. No caller value is ever interpolated into the statement.
        sql = "SELECT payload FROM shots"
        params: tuple[Any, ...] = ()
        if character_id is not None:
            sql += " WHERE character_id=?"
            params = (character_id,)
        rows = self._conn.execute(sql + " ORDER BY shot_id", params).fetchall()
        return tuple(_shot_from(json.loads(row["payload"])) for row in rows)

    def shot(self, shot_id: str) -> ShotReference:
        row = self._conn.execute("SELECT payload FROM shots WHERE shot_id=?", (shot_id,)).fetchone()
        if row is None:
            raise UnknownReference(f"unknown shot {shot_id!r}", shot_id=shot_id)
        return _shot_from(json.loads(row["payload"]))

    # -- consent ----------------------------------------------------------
    def add_consent(
        self, record: ConsentRecord, *, principal: Principal | None = None
    ) -> ConsentRecord:
        if principal is not None:
            self.access.require(
                principal, Permission.MANAGE_CONSENT, character_id=record.character_id
            )
        existing = self.consents.get(record.consent_id)
        if existing is not None:
            # ``ConsentLedger.add`` enforces the terminal-revocation rule.
            self.consents.add(record)
            return existing
        stored = self.consents.add(record)
        try:
            self._conn.execute(
                "INSERT INTO consents(consent_id, character_id, payload) VALUES (?,?,?)",
                (record.consent_id, record.character_id, canonical_json(record.to_dict())),
            )
            self._append_lineage("consent.recorded", record.to_dict())
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return stored

    def revoke_consent(
        self, consent_id: str, reason: str, *, principal: Principal | None = None
    ) -> ConsentRecord:
        record = self.consents.get(consent_id)
        if record is None:
            raise ConsentRequired(f"unknown consent {consent_id!r}", consent_id=consent_id)
        if principal is not None:
            self.access.require(
                principal, Permission.MANAGE_CONSENT, character_id=record.character_id
            )
        revoked = self.consents.revoke(consent_id, reason)
        digest = scoped_digest("consent-revocation", revoked.to_dict())
        try:
            self._conn.execute(
                "UPDATE consents SET payload=? WHERE consent_id=?",
                (canonical_json(revoked.to_dict()), consent_id),
            )
            self._conn.execute(
                "INSERT INTO consent_revocations(consent_id, character_id, revoked_at, reason, "
                "record_digest) VALUES (?,?,?,?,?)",
                (consent_id, revoked.character_id, revoked.revoked_at, reason, digest),
            )
            self._append_lineage(
                "consent.revoked",
                {"consent_id": consent_id, "reason": reason, "digest": digest},
            )
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return revoked

    def revocations(self, *, character_id: str | None = None) -> tuple[dict[str, Any], ...]:
        sql = "SELECT * FROM consent_revocations"
        params: tuple[Any, ...] = ()
        if character_id is not None:
            sql += " WHERE character_id=?"
            params = (character_id,)
        rows = self._conn.execute(sql + " ORDER BY revoked_at", params).fetchall()
        return tuple(dict(row) for row in rows)

    def consent_state(self, character_id: str) -> dict[str, Any]:
        records = self.consents.for_character(character_id)
        return {
            "character_id": character_id,
            "records": [record.to_dict() for record in records],
            "revoked_all": bool(records) and all(record.is_revoked for record in records),
            "revocations": list(self.revocations(character_id=character_id)),
        }

    def consent_decision(
        self,
        character_id: str,
        purpose: ConsentPurpose = ConsentPurpose.EXPORT,
        *,
        right: str | None = None,
        territory: str | None = None,
    ) -> ConsentDecision:
        return self.consents.decide(character_id, purpose, right=right, territory=territory)

    # -- imported consent (quarantine) ------------------------------------
    def imported_consents(self, *, character_id: str | None = None) -> tuple[dict[str, Any], ...]:
        sql = "SELECT * FROM consent_imports"
        params: tuple[Any, ...] = ()
        if character_id is not None:
            sql += " WHERE character_id=?"
            params = (character_id,)
        rows = self._conn.execute(sql + " ORDER BY consent_id", params).fetchall()
        return tuple(dict(row) for row in rows)

    def adopt_imported_consent(self, consent_id: str, *, principal: Principal) -> ConsentRecord:
        """Promote a quarantined consent record after an explicit human decision.

        Import never mints authorization: a bundle's consent records land here and
        stay inert until someone with ``MANAGE_CONSENT`` adopts them. Adoption
        still requires a documented subject reference and a document digest, so a
        hand-written JSON blob cannot become a legal basis by being imported.
        """
        row = self._conn.execute(
            "SELECT * FROM consent_imports WHERE consent_id=?", (consent_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference(f"no imported consent {consent_id!r}", consent_id=consent_id)
        record = ConsentRecord.from_dict(json.loads(row["payload"]))
        self.access.require(principal, Permission.MANAGE_CONSENT, character_id=record.character_id)
        if not record.document_digest:
            raise ValidationError(
                "an imported consent without a document digest cannot be adopted",
                consent_id=consent_id,
            )
        if int(row["adopted"]):
            raise ValidationError("consent was already adopted", consent_id=consent_id)
        stored = self.consents.add(record)
        try:
            self._conn.execute(
                "INSERT INTO consents(consent_id, character_id, payload) VALUES (?,?,?)",
                (record.consent_id, record.character_id, canonical_json(record.to_dict())),
            )
            self._conn.execute(
                "UPDATE consent_imports SET adopted=1, adopted_by=? WHERE consent_id=?",
                (principal.principal_id, consent_id),
            )
            self._append_lineage(
                "consent.adopted",
                {
                    "consent_id": consent_id,
                    "principal": principal.principal_id,
                    "document_digest": record.document_digest,
                },
            )
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return stored

    # -- lineage ----------------------------------------------------------
    def _append_lineage(self, kind: str, payload: Mapping[str, Any]) -> str:
        row = self._conn.execute(
            "SELECT record_hash FROM lineage ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["record_hash"] if row else "sha256:" + ("0" * 64)
        body = {"kind": kind, "payload": dict(payload), "prev_hash": prev_hash}
        record_hash = scoped_digest(SCOPE_LINEAGE, body)
        self._conn.execute(
            "INSERT INTO lineage(kind, payload, prev_hash, record_hash) VALUES (?,?,?,?)",
            (kind, canonical_json(dict(payload)), prev_hash, record_hash),
        )
        return record_hash

    def lineage(self) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            "SELECT seq, kind, payload, prev_hash, record_hash FROM lineage ORDER BY seq"
        ).fetchall()
        return tuple(
            {
                "seq": int(row["seq"]),
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "prev_hash": row["prev_hash"],
                "record_hash": row["record_hash"],
            }
            for row in rows
        )

    def verify_lineage(self) -> dict[str, Any]:
        """Recompute every link; report the first broken position."""
        previous = "sha256:" + ("0" * 64)
        entries = self.lineage()
        for entry in entries:
            body = {
                "kind": entry["kind"],
                "payload": entry["payload"],
                "prev_hash": previous,
            }
            expected = scoped_digest(SCOPE_LINEAGE, body)
            if entry["prev_hash"] != previous or entry["record_hash"] != expected:
                return {"ok": False, "broken_at": entry["seq"], "expected": expected}
            previous = entry["record_hash"]
        return {"ok": True, "length": len(entries), "head": previous}

    # -- audit ------------------------------------------------------------
    def record_audit(
        self,
        principal: Principal,
        action: str,
        *,
        allowed: bool,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO audit(at, principal, role, action, allowed, detail) VALUES (?,?,?,?,?,?)",
            (
                self._now(),
                principal.principal_id,
                principal.role.value,
                action,
                int(allowed),
                canonical_json(dict(detail or {})),
            ),
        )
        self._conn.commit()

    def audit_events(self) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            "SELECT at, principal, role, action, allowed, detail FROM audit ORDER BY seq"
        ).fetchall()
        return tuple(
            {
                "at": row["at"],
                "principal": row["principal"],
                "role": row["role"],
                "action": row["action"],
                "allowed": bool(row["allowed"]),
                "detail": json.loads(row["detail"]),
            }
            for row in rows
        )

    # -- export -----------------------------------------------------------
    def _authorize_export(
        self,
        character_id: str,
        *,
        principal: Principal,
        purpose: ConsentPurpose,
        right: str,
        territory: str | None,
    ) -> ConsentDecision:
        """Permission first, then consent — before a single byte leaves the store."""
        self.access.require(principal, Permission.EXPORT, character_id=character_id)
        subject = self.subject_class(character_id)
        if subject is SubjectClass.SYNTHETIC:
            decision = ConsentDecision(
                allowed=True,
                reason="synthetic subject: consent is not applicable",
                character_id=character_id,
                purpose=purpose.value,
                right=right,
                territory=territory or "",
            )
        else:
            decision = self.consent_decision(
                character_id, purpose, right=right, territory=territory
            )
        self.record_audit(
            principal,
            "export:authorized" if decision.allowed else "export:refused_consent",
            allowed=decision.allowed,
            detail={"character_id": character_id, "consent": decision.to_dict()},
        )
        if not decision.allowed:
            raise ExportBlocked(
                f"export refused: {decision.reason}",
                character_id=character_id,
                purpose=purpose.value,
                right=right,
                territory=territory,
            )
        return decision

    def export_bundle(
        self,
        character_id: str,
        destination: str | Path,
        *,
        principal: Principal,
        purpose: ConsentPurpose | str = ConsentPurpose.EXPORT,
        right: str = "export",
        territory: str | None = None,
    ) -> Path:
        """Portable bundle: capsule JSON + decrypted asset bytes + a manifest.

        Written into a staging directory and swapped in at the end: a failure at
        any point leaves the previous destination untouched.
        """
        purpose_enum = ConsentPurpose.coerce(purpose)
        identity = self.identity(character_id)
        capsule = self.load_capsule(character_id)
        decision = self._authorize_export(
            character_id,
            principal=principal,
            purpose=purpose_enum,
            right=right,
            territory=territory,
        )
        target = Path(destination)
        staging = target.parent / f".{target.name}.staging-{os.getpid()}-{secrets.token_hex(4)}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            (staging / "assets").mkdir()
            manifest: dict[str, Any] = {
                "schema": BUNDLE_SCHEMA,
                "exported_at": self._now(),
                "purpose": purpose_enum.value,
                "right": right,
                "territory": territory,
                "consent_decision": decision.to_dict(),
                "storage_policy": self.storage_policy(),
                "identity": identity.payload(),
                "capsule": capsule.to_dict(),
                "consent": self.consent_state(character_id),
                "assets": [],
            }
            for ref in capsule.asset_refs:
                row = self._asset_row(ref.asset_id)
                if row["character_id"] != character_id:
                    raise AccessDenied(
                        "capsule references an asset owned by another character",
                        asset_id=ref.asset_id,
                        owner=row["character_id"],
                        requested=character_id,
                    )
                data = self.asset_bytes(ref.asset_id, character_id=character_id)
                name = f"{ref.asset_id}-{ref.sha256.split(':', 1)[-1][:16]}.bin"
                (staging / "assets" / name).write_bytes(data)
                manifest["assets"].append(
                    {**ref.to_dict(), "file": f"assets/{name}", "bundle_sha256": sha256_hex(data)}
                )
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            self._swap_in(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.record_audit(
            principal,
            "export:written",
            allowed=True,
            detail={
                "character_id": character_id,
                "assets": len(capsule.asset_refs),
                "purpose": purpose_enum.value,
            },
        )
        self._append_lineage(
            "capsule.exported",
            {
                "character_id": character_id,
                "purpose": purpose_enum.value,
                "consent_id": decision.consent_id,
            },
        )
        self._conn.commit()
        return target

    @staticmethod
    def _swap_in(staging: Path, target: Path) -> None:
        """Publish ``staging`` at ``target`` without ever losing the old directory."""
        previous = target.parent / f".{target.name}.previous-{secrets.token_hex(4)}"
        moved_previous = False
        if target.exists():
            os.replace(target, previous)
            moved_previous = True
        try:
            os.replace(staging, target)
        except BaseException:
            if moved_previous:
                os.replace(previous, target)
            raise
        if moved_previous:
            shutil.rmtree(previous, ignore_errors=True)

    # -- import -----------------------------------------------------------
    def import_bundle(
        self,
        source: str | Path,
        *,
        character_id: str | None = None,
        principal: Principal | None = None,
    ) -> CharacterCapsule:
        """Import a bundle after validating the whole thing; then write atomically.

        Nothing is written until every asset has been read from a path that is
        provably inside the bundle, matched against its recorded digest, and the
        capsule has been re-sealed and verified.
        """
        root = Path(source)
        if not root.is_dir():
            raise ValidationError("bundle directory does not exist", path=str(root))
        root = root.resolve()
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise ValidationError("bundle has no manifest.json", path=str(root))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != BUNDLE_SCHEMA:
            raise ValidationError("unsupported bundle schema", schema=manifest.get("schema"))
        capsule = capsule_from_payload(manifest["capsule"])
        capsule.verify()
        if character_id is not None and character_id != capsule.character_id:
            raise ValidationError(
                "bundle character does not match the requested id", character_id=character_id
            )
        if principal is not None:
            self.access.require(
                principal, Permission.CREATE_CAPSULE, character_id=capsule.character_id
            )
        identity_payload = manifest["identity"]
        if identity_payload["character_id"] != capsule.character_id:
            raise ValidationError(
                "bundle identity and capsule disagree",
                identity=identity_payload["character_id"],
                capsule=capsule.character_id,
            )

        # 1. validate every asset (containment + digest + type) before writing.
        staged: list[tuple[dict[str, Any], bytes]] = []
        seen_ids: set[str] = set()
        for entry in manifest.get("assets", []):
            asset_id = str(entry["asset_id"])
            if asset_id in seen_ids:
                raise ValidationError("bundle lists the same asset twice", asset_id=asset_id)
            seen_ids.add(asset_id)
            data = self._read_bundle_file(root, str(entry["file"]))
            if sha256_hex(data) != entry["sha256"] or len(data) != int(entry["size_bytes"]):
                raise IntegrityError(
                    "bundle asset does not match its recorded digest/size", asset_id=asset_id
                )
            media_type = sniff_media_type(data)
            if media_type is None or media_type != entry["media_type"]:
                raise MediaUnsupported(
                    "bundle asset type is not the recorded type",
                    asset_id=asset_id,
                    recorded=entry["media_type"],
                    detected=media_type,
                )
            staged.append((entry, data))

        # 2. validate the capsule's own references.
        if sorted(ref.asset_id for ref in capsule.asset_refs) != sorted(seen_ids):
            raise ValidationError(
                "bundle assets do not match the assets pinned by the capsule",
                pinned=sorted(ref.asset_id for ref in capsule.asset_refs),
                present=sorted(seen_ids),
            )

        # 3. write: character (if new), assets, capsule revision, consent quarantine.
        existing = capsule.character_id in self.character_ids()
        try:
            if not existing:
                self._conn.execute(
                    "INSERT INTO characters(character_id, payload, created_at) VALUES (?,?,?)",
                    (
                        capsule.character_id,
                        canonical_json(
                            CharacterIdentity(
                                character_id=capsule.character_id,
                                display_name=identity_payload["display_name"],
                                subject_class=SubjectClass(identity_payload["subject_class"]),
                                work_title=identity_payload["work_title"],
                                created_at=identity_payload["created_at"],
                            ).payload()
                        ),
                        self._now(),
                    ),
                )
                self._append_lineage("character.imported", identity_payload)
                current_revision = 0
            else:
                row = self._conn.execute(
                    "SELECT revision_no FROM capsules WHERE character_id=?",
                    (capsule.character_id,),
                ).fetchone()
                current_revision = int(row["revision_no"]) if row else 0
                if capsule.revision_no <= current_revision:
                    raise RevisionConflict(
                        "imported capsule revision is not newer than the stored one",
                        stored=current_revision,
                        imported=capsule.revision_no,
                    )
            for entry, data in staged:
                record = self.blobs.put(
                    data,
                    declared_name=str(entry.get("display_name", "")),
                    purpose="reference-asset",
                )
                row = self._conn.execute(
                    "SELECT asset_id FROM assets WHERE character_id=? AND sha256=? AND role=?",
                    (capsule.character_id, record.sha256, entry["role"]),
                ).fetchone()
                if row is not None:
                    continue
                self._conn.execute(
                    "INSERT INTO assets(asset_id, character_id, sha256, size_bytes, media_type, "
                    "role, display_name, encrypted, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        entry["asset_id"],
                        capsule.character_id,
                        record.sha256,
                        record.size_bytes,
                        record.media_type,
                        entry["role"],
                        str(entry.get("display_name", "")),
                        int(record.encrypted),
                        self._now(),
                    ),
                )
            payload = canonical_json(capsule.to_dict())
            self._conn.execute(
                "INSERT INTO capsule_revisions(character_id, revision_no, digest, payload, "
                "created_at) VALUES (?,?,?,?,?)",
                (
                    capsule.character_id,
                    capsule.revision_no,
                    capsule.digest,
                    payload,
                    self._now(),
                ),
            )
            self._conn.execute(
                "INSERT INTO capsules(character_id, revision_no, digest, payload) VALUES (?,?,?,?) "
                "ON CONFLICT(character_id) DO UPDATE SET revision_no=excluded.revision_no, "
                "digest=excluded.digest, payload=excluded.payload",
                (capsule.character_id, capsule.revision_no, capsule.digest, payload),
            )
            bundle_digest = sha256_hex(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            for record_payload in (manifest.get("consent") or {}).get("records", []):
                record = ConsentRecord.from_dict(record_payload)
                if record.character_id != capsule.character_id:
                    continue
                self._conn.execute(
                    "INSERT OR IGNORE INTO consent_imports(consent_id, character_id, payload, "
                    "source_bundle_digest, imported_at, adopted, adopted_by) "
                    "VALUES (?,?,?,?,?,0,'')",
                    (
                        record.consent_id,
                        record.character_id,
                        canonical_json(record.to_dict()),
                        bundle_digest,
                        self._now(),
                    ),
                )
            self._append_lineage(
                "capsule.imported",
                {
                    "character_id": capsule.character_id,
                    "revision_no": capsule.revision_no,
                    "bundle_digest": bundle_digest,
                },
            )
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()
        return self.load_capsule(capsule.character_id)

    @staticmethod
    def _read_bundle_file(root: Path, relative: str) -> bytes:
        """Read a bundle-relative file, refusing anything outside the bundle root."""
        text = str(relative)
        if not text:
            raise ValidationError("bundle asset entry has no file")
        candidate = Path(text)
        if candidate.is_absolute() or text.startswith(("/", "\\")) or ".." in candidate.parts:
            raise ValidationError(
                "bundle asset path must be relative and inside the bundle", file=text
            )
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root):
            raise ValidationError(
                "bundle asset path escapes the bundle directory", file=text, resolved=str(resolved)
            )
        if not resolved.is_file():
            raise ValidationError("bundle asset file is missing", file=text)
        return resolved.read_bytes()


def _environment_from(data: Mapping[str, Any] | None) -> Any:
    from .scene import Environment

    if not data:
        return Environment()
    return Environment(
        background_colour=data.get("background_colour", "#3a3f4b"),
        key_light_kelvin=int(data.get("key_light_kelvin", 5600)),
        intensity=float(data.get("intensity", 1.0)),
    )


def _shot_from(data: Mapping[str, Any]) -> ShotReference:
    from .scene import DeclaredShot, OverrideDimension, SceneOverride

    declared = data["declared"]
    override_payload = declared.get("scene_override")
    override = None
    if override_payload:
        override = SceneOverride(
            scene_id=override_payload["scene_id"],
            choices={
                OverrideDimension(dimension): value
                for dimension, value in override_payload["choices"].items()
            },
            reason=override_payload["reason"],
        )
    return ShotReference(
        shot_id=data["shot_id"],
        scene_id=data["scene_id"],
        sequence_id=data["sequence_id"],
        shot_index=int(data["shot_index"]),
        character_id=data["character_id"],
        capsule_revision=int(data["capsule_revision"]),
        capsule_digest=data["capsule_digest"],
        module_digests=data["module_digests"],
        declared=DeclaredShot(
            outfit_id=declared["outfit_id"],
            expression=declared.get("expression", "neutral"),
            pose=declared.get("pose", "standing"),
            canon_assertions=declared.get("canon_assertions") or {},
            scene_override=override,
        ),
        image_asset_id=data["image_asset_id"],
        voice_asset_id=data.get("voice_asset_id"),
        motion_asset_id=data.get("motion_asset_id"),
    )


__all__ = [
    "ALLOWED_MEDIA_TYPES",
    "BUNDLE_SCHEMA",
    "SCHEMA_VERSION",
    "BlobRecord",
    "BlobStore",
    "CapsuleRepository",
    "canon_from_payload",
    "capsule_from_payload",
    "wardrobe_from_payload",
]
