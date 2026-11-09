"""Durable storage: content-addressed blobs, a SQLite capsule repository, lineage.

Design notes
------------
* **Content addressing, measured not declared.** The digest of a reference asset
  is computed from the bytes actually received. A caller's filename or MIME type
  never influences the recorded digest or the accepted type, and the filename is
  never used as a path.
* **One repository, no parallel ledger.** Revisions are compare-and-swap on
  ``revision_no``; a gap or a rewrite is a typed ``RevisionConflict``. Lineage is
  a domain-separated hash chain local to this island; it is *evidence*, not a
  second authority over NEXUS's causal journal (the integration contract says so
  explicitly).
* **SQLite + filesystem only.** No queue, no message bus, no second registry.

The prototype deliberately uses ``sqlite3`` and ``pathlib`` rather than the
repository's SQLAlchemy models: it must stay runnable and auditable in isolation
from the Arena-1 runtime, and it must not create a second schema inside NEXUS's
database.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canon import CanonFact, CanonModule, FactKind, StoryEvent
from .canonical import SCOPE_LINEAGE, canonical_json, scoped_digest, sha256_hex
from .errors import (
    AssetRejected,
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
    VoiceModule,
    next_character_id,
)
from .scene import Scene, ShotReference, Timeline, validate_shot_reference
from .security import (
    AccessPolicy,
    Cipher,
    ConsentLedger,
    ConsentRecord,
    Permission,
    Principal,
    _utc_now,
)
from .wardrobe import Outfit, WardrobeChangeEvent, WardrobeModule

SCHEMA_VERSION = 1
DEFAULT_MAX_ASSET_BYTES = 16 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{0,128}$")
ALLOWED_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "audio/wav"})


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "encrypted": self.encrypted,
        }


class BlobStore:
    """Digest-named blob directory with optional authenticated encryption."""

    def __init__(
        self,
        root: str | Path,
        *,
        cipher: Cipher | None = None,
        max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cipher = cipher
        self.max_bytes = int(max_bytes)

    # -- paths ------------------------------------------------------------
    def path_for(self, digest: str) -> Path:
        hexdigest = digest.split(":", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{64}", hexdigest):
            raise AssetRejected("malformed digest", digest=digest)
        return self.root / hexdigest[:2] / hexdigest[2:4] / hexdigest

    @staticmethod
    def _header(*, purpose: str, digest: str, context: Mapping[str, str]) -> dict[str, Any]:
        return {"purpose": purpose, "sha256": digest, "context": dict(sorted(context.items()))}

    # -- write ------------------------------------------------------------
    def put(
        self,
        data: bytes,
        *,
        declared_name: str = "",
        purpose: str = "reference-asset",
        context: Mapping[str, str] | None = None,
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
        digest = sha256_hex(bytes(data))
        header = self._header(purpose=purpose, digest=digest, context=context or {})
        payload = (
            bytes(data) if self.cipher is None else self.cipher.encrypt(bytes(data), header=header)
        )
        target = self.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_bytes()
            if existing != payload:
                raise IntegrityError(
                    "a different blob is already stored under this digest (collision or tamper)",
                    digest=digest,
                )
        else:
            staging = target.with_suffix(".staging")
            staging.write_bytes(payload)
            staging.replace(target)
        return BlobRecord(
            sha256=digest,
            size_bytes=len(data),
            media_type=media_type,
            encrypted=self.cipher is not None,
        )

    # -- read -------------------------------------------------------------
    def read(
        self,
        record: BlobRecord,
        *,
        purpose: str = "reference-asset",
        context: Mapping[str, str] | None = None,
    ) -> bytes:
        path = self.path_for(record.sha256)
        if not path.exists():
            raise UnknownReference("blob is missing from the store", digest=record.sha256)
        raw = path.read_bytes()
        if self.cipher is None:
            data = raw
        else:
            header = self._header(purpose=purpose, digest=record.sha256, context=context or {})
            data = self.cipher.decrypt(raw, header=header)
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

    def verify(
        self,
        record: BlobRecord,
        *,
        purpose: str = "reference-asset",
        context: Mapping[str, str] | None = None,
    ) -> None:
        """Re-measure the stored bytes; raises on any mismatch."""
        self.read(record, purpose=purpose, context=context)


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
        key: Outfit(**{**value, "accessories": tuple(value.get("accessories", ()))})
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
    created_at TEXT NOT NULL
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
        max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
        now: Any = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "capsules.sqlite3"
        self.blobs = BlobStore(self.root / "blobs", cipher=cipher, max_bytes=max_asset_bytes)
        self.consents = ConsentLedger(now=now)
        self.access = AccessPolicy()
        self._now = now or _utc_now
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

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
        from .model import SubjectClass

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
        self._conn.execute(
            "INSERT INTO capsule_revisions(character_id, revision_no, digest, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (capsule.character_id, capsule.revision_no, capsule.digest, payload, self._now()),
        )
        self._conn.execute(
            "INSERT INTO capsules(character_id, revision_no, digest, payload) VALUES (?,?,?,?) "
            "ON CONFLICT(character_id) DO UPDATE SET "
            "revision_no=excluded.revision_no, digest=excluded.digest, payload=excluded.payload",
            (capsule.character_id, capsule.revision_no, capsule.digest, payload),
        )
        self._append_lineage("capsule.revision", capsule.payload())
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
        digest = scoped_digest(
            "identity",
            {
                "character_id": capsule.character_id,
                "version": capsule.identity_version,
                "face": capsule.module("face").payload(),
                "body": capsule.module("body").payload(),
                "note": note,
            },
        )
        self._conn.execute(
            "INSERT INTO identity_versions(character_id, version, digest, payload) "
            "VALUES (?,?,?,?)",
            (
                capsule.character_id,
                capsule.identity_version,
                digest,
                canonical_json(
                    {
                        "character_id": capsule.character_id,
                        "version": capsule.identity_version,
                        "face": capsule.module("face").payload(),
                        "body": capsule.module("body").payload(),
                        "note": note,
                    }
                ),
            ),
        )
        self._append_lineage(
            "identity.version", {"digest": digest, "version": capsule.identity_version}
        )
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
        identity = self.identity(character_id)
        record = self.blobs.put(
            data,
            declared_name=display_name,
            purpose=purpose,
            context={"character_id": identity.character_id, "role": role},
        )
        asset_id = self._next_asset_id()
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

    def asset_bytes(self, asset_id: str) -> bytes:
        row = self._conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if row is None:
            raise UnknownReference(f"unknown asset {asset_id!r}", asset_id=asset_id)
        record = BlobRecord(
            sha256=row["sha256"],
            size_bytes=int(row["size_bytes"]),
            media_type=row["media_type"],
            encrypted=bool(row["encrypted"]),
        )
        return self.blobs.read(
            record,
            purpose="reference-asset",
            context={"character_id": row["character_id"], "role": row["role"]},
        )

    def asset_record(self, asset_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if row is None:
            raise UnknownReference(f"unknown asset {asset_id!r}", asset_id=asset_id)
        return dict(row)

    def verify_integrity(self, character_id: str) -> dict[str, Any]:
        """Re-measure every asset of a character; report, never silently pass."""
        capsule = self.load_capsule(character_id)
        checked: list[str] = []
        failures: list[dict[str, Any]] = []
        for ref in capsule.asset_refs:
            checked.append(ref.asset_id)
            try:
                data = self.asset_bytes(ref.asset_id)
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
    def add_consent(self, record: ConsentRecord) -> ConsentRecord:
        self.consents.add(record)
        self._conn.execute(
            "INSERT INTO consents(consent_id, character_id, payload) VALUES (?,?,?) "
            "ON CONFLICT(consent_id) DO UPDATE SET payload=excluded.payload",
            (record.consent_id, record.character_id, canonical_json(record.to_dict())),
        )
        self._append_lineage("consent.recorded", record.to_dict())
        self._conn.commit()
        return record

    def revoke_consent(self, consent_id: str, reason: str) -> ConsentRecord:
        record = self.consents.revoke(consent_id, reason)
        self._conn.execute(
            "UPDATE consents SET payload=? WHERE consent_id=?",
            (canonical_json(record.to_dict()), consent_id),
        )
        self._append_lineage("consent.revoked", {"consent_id": consent_id, "reason": reason})
        self._conn.commit()
        return record

    def consent_state(self, character_id: str) -> dict[str, Any]:
        records = self.consents.for_character(character_id)
        return {
            "character_id": character_id,
            "records": [record.to_dict() for record in records],
            "revoked_all": bool(records) and all(record.revoked_at for record in records),
        }

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
        for entry in self.lineage():
            body = {
                "kind": entry["kind"],
                "payload": entry["payload"],
                "prev_hash": previous,
            }
            expected = scoped_digest(SCOPE_LINEAGE, body)
            if entry["prev_hash"] != previous or entry["record_hash"] != expected:
                return {"ok": False, "broken_at": entry["seq"], "expected": expected}
            previous = entry["record_hash"]
        return {"ok": True, "length": len(self.lineage()), "head": previous}

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

    # -- export / import --------------------------------------------------
    def export_bundle(
        self,
        character_id: str,
        destination: str | Path,
        *,
        principal: Principal,
        purpose: str = "EXPORT",
    ) -> Path:
        """Portable bundle: capsule JSON + decrypted asset bytes + a manifest."""
        identity = self.identity(character_id)
        capsule = self.load_capsule(character_id)
        state = self.consent_state(character_id)
        if identity.subject_class.value == "REAL_PERSON" and state["revoked_all"]:
            self.record_audit(
                principal,
                "export:blocked_revoked_consent",
                allowed=False,
                detail={"character_id": character_id},
            )
            raise ExportBlocked("export refused: consent is revoked", character_id=character_id)
        target = Path(destination)
        if target.exists():
            shutil.rmtree(target)
        (target / "assets").mkdir(parents=True)
        manifest: dict[str, Any] = {
            "schema": "character-capsule-bundle/1",
            "exported_at": self._now(),
            "purpose": purpose,
            "identity": identity.payload(),
            "capsule": capsule.to_dict(),
            "consent": state,
            "assets": [],
        }
        for ref in capsule.asset_refs:
            data = self.asset_bytes(ref.asset_id)
            name = f"{ref.asset_id}-{ref.sha256.split(':', 1)[-1][:16]}.bin"
            (target / "assets" / name).write_bytes(data)
            manifest["assets"].append(
                {**ref.to_dict(), "file": f"assets/{name}", "bundle_sha256": sha256_hex(data)}
            )
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        self.record_audit(
            principal, "export:written", allowed=True, detail={"character_id": character_id}
        )
        self._append_lineage("capsule.exported", {"character_id": character_id, "purpose": purpose})
        self._conn.commit()
        return target

    def import_bundle(
        self, source: str | Path, *, character_id: str | None = None
    ) -> CharacterCapsule:
        """Import a bundle into this store, re-measuring every asset digest."""
        target = Path(source)
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema") != "character-capsule-bundle/1":
            raise ValidationError("unsupported bundle schema", schema=manifest.get("schema"))
        capsule = capsule_from_payload(manifest["capsule"])
        if character_id is not None and character_id != capsule.character_id:
            raise ValidationError(
                "bundle character does not match the requested id", character_id=character_id
            )
        if capsule.character_id not in self.character_ids():
            from .model import SubjectClass

            identity = manifest["identity"]
            self.create_character(
                display_name=identity["display_name"],
                subject_class=SubjectClass(identity["subject_class"]),
                work_title=identity["work_title"],
                character_id=capsule.character_id,
            )
        refs: list[AssetRef] = []
        for entry in manifest["assets"]:
            data = (target / entry["file"]).read_bytes()
            if sha256_hex(data) != entry["sha256"]:
                raise IntegrityError(
                    "bundle asset digest does not match the manifest",
                    asset_id=entry["asset_id"],
                )
            refs.append(self.register_asset(capsule.character_id, data, role=entry["role"]))
        stored = self.load_capsule(capsule.character_id)
        return self.save_capsule(stored.with_assets(refs))


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
