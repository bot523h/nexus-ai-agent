"""Wave 2 — cryptographic revision & asset-version pinning.

Every test drives a real SQLite/WAL store and real files on disk: the pinning
layer exists to catch *real* drift, so a mock would test nothing.  Coverage
target for ``revision_pinning``: ``>= 98%`` line *and* branch.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.studio.revision_pinning import (
    AssetPinViolationError,
    AssetVersionPin,
    DurableRevisionPinningStore,
    PinnedRevisionEnvelope,
    PinnedRevisionState,
    RevisionConcurrencyConflictError,
    RevisionDriftError,
    RevisionIdempotencyConflictError,
    RevisionNotFoundError,
    RevisionPinningError,
    sha256_file_hex,
    sha256_pinned_file,
    utc_now,
)
from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_digest,
)

PROJECT = "proj-cinema-1"
TENANT = "tenant-studio-1"
H = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


class _Clock:
    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> str:
        self._tick += 1
        return f"2026-11-26T01:00:{self._tick:02d}+00:00"


def _store(tmp_path: Path, *, now: Any = None) -> DurableRevisionPinningStore:
    return DurableRevisionPinningStore(tmp_path / "pins.sqlite3", now=now or _Clock())


def _pin_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "project_id": PROJECT,
        "tenant_id": TENANT,
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "revision_number": 1,
        "parent_revision_id": None,
        "plan_id": "plan-1",
        "plan_jcs_sha256": H,
        "parameters_jcs_sha256": H2,
        "preview_digest": H3,
        "input_asset_pins": (),
    }
    kwargs.update(overrides)
    return kwargs


def _asset_pin(asset_id: str = "asset-a", **overrides: Any) -> AssetVersionPin:
    kwargs: dict[str, Any] = {
        "asset_id": asset_id,
        "relative_path": f"media/{asset_id}.mp4",
        "content_sha256": H,
        "byte_size": 5,
    }
    kwargs.update(overrides)
    return AssetVersionPin(**kwargs)


def _write_asset(root: Path, name: str = "media/intro.mp4", payload: bytes = b"hello") -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


# --------------------------------------------------------------------------- #
# AssetVersionPin invariants
# --------------------------------------------------------------------------- #
class TestAssetVersionPin:
    def test_accepts_a_well_formed_pin_and_is_frozen(self) -> None:
        pin = _asset_pin()
        assert pin.content_sha256 == H
        assert pin.to_canonical_dict() == {
            "asset_id": "asset-a",
            "relative_path": "media/asset-a.mp4",
            "content_sha256": H,
            "byte_size": 5,
            "media_type": "application/octet-stream",
        }
        with pytest.raises(dataclasses.FrozenInstanceError):
            pin.byte_size = 9  # type: ignore[misc]

    @pytest.mark.parametrize(
        "overrides,reason",
        [
            ({"asset_id": ""}, "invalid_field"),
            ({"asset_id": "   "}, "invalid_field"),
            ({"asset_id": 7}, "invalid_field"),
            ({"relative_path": ""}, "invalid_field"),
            ({"relative_path": "/etc/passwd"}, "invalid_relative_path"),
            ({"relative_path": "media/../../etc/passwd"}, "invalid_relative_path"),
            ({"content_sha256": "A" * 64}, "invalid_digest"),
            ({"content_sha256": "abc"}, "invalid_digest"),
            ({"byte_size": -1}, "invalid_byte_size"),
            ({"byte_size": True}, "invalid_byte_size"),
            ({"byte_size": "5"}, "invalid_byte_size"),
            ({"media_type": ""}, "invalid_media_type"),
            ({"media_type": 3}, "invalid_media_type"),
        ],
    )
    def test_rejects_malformed_fields(self, overrides: dict[str, Any], reason: str) -> None:
        with pytest.raises(RevisionPinningError) as excinfo:
            _asset_pin(**overrides)
        assert excinfo.value.reason == reason

    def test_zero_byte_asset_is_legal(self) -> None:
        assert _asset_pin(byte_size=0).byte_size == 0


# --------------------------------------------------------------------------- #
# Envelope invariants
# --------------------------------------------------------------------------- #
class TestPinnedRevisionEnvelope:
    def _envelope(self, **overrides: Any) -> PinnedRevisionEnvelope:
        kwargs: dict[str, Any] = {
            "revision_id": "rev-1",
            "project_id": PROJECT,
            "tenant_id": TENANT,
            "session_id": "sess-1",
            "turn_id": "turn-1",
            "revision_number": 1,
            "parent_revision_id": None,
            "plan_id": "plan-1",
            "plan_jcs_sha256": H,
            "parameters_jcs_sha256": H2,
            "preview_digest": H3,
            "input_asset_pins": (),
            "revision_jcs_sha256": "",
            "created_at_utc": "2026-11-26T00:00:00+00:00",
        }
        kwargs.update(overrides)
        return PinnedRevisionEnvelope(**kwargs)

    def test_digest_is_jcs_over_all_fields_and_verifies(self) -> None:
        envelope = self._envelope()
        expected = canonical_digest(
            envelope.canonical_payload(include_digest=False),
            CanonicalizationScheme.JCS_RFC8785_V2,
        )
        assert envelope.revision_jcs_sha256 == expected
        assert envelope.verify_envelope_integrity() is True
        assert envelope.to_canonical_dict()["revision_jcs_sha256"] == expected
        assert "revision_jcs_sha256" not in envelope.canonical_payload()

    def test_asset_pins_are_coerced_to_a_sorted_tuple(self) -> None:
        envelope = self._envelope(
            input_asset_pins=[_asset_pin("zeta"), _asset_pin("alpha"), _asset_pin("mike")]
        )
        assert isinstance(envelope.input_asset_pins, tuple)
        assert [pin.asset_id for pin in envelope.input_asset_pins] == ["alpha", "mike", "zeta"]
        # Order of the *input* cannot change the digest.
        left = self._envelope(input_asset_pins=[_asset_pin("alpha"), _asset_pin("zeta")])
        right = self._envelope(
            input_asset_pins=tuple(reversed([_asset_pin("zeta"), _asset_pin("alpha")]))
        )
        assert left.revision_jcs_sha256 == right.revision_jcs_sha256

    def test_bare_hex_digests_are_normalised_to_the_prefixed_spelling(self) -> None:
        envelope = self._envelope(plan_jcs_sha256=H, preview_digest=H2)
        assert envelope.plan_jcs_sha256 == f"sha256:{H}"
        assert envelope.preview_digest == f"sha256:{H2}"
        prefixed = self._envelope(plan_jcs_sha256=f"sha256:{H}", preview_digest=f"sha256:{H2}")
        assert prefixed.revision_jcs_sha256 == envelope.revision_jcs_sha256

    @pytest.mark.parametrize(
        "overrides,reason",
        [
            ({"revision_id": ""}, "invalid_field"),
            ({"project_id": "  "}, "invalid_field"),
            ({"tenant_id": ""}, "invalid_field"),
            ({"session_id": ""}, "invalid_field"),
            ({"turn_id": ""}, "invalid_field"),
            ({"plan_id": ""}, "invalid_field"),
            ({"created_at_utc": ""}, "invalid_field"),
            ({"parent_revision_id": ""}, "invalid_field"),
            ({"plan_jcs_sha256": "zz"}, "invalid_digest"),
            ({"parameters_jcs_sha256": "sha256:xyz"}, "invalid_digest"),
            ({"preview_digest": 12}, "invalid_digest"),
            ({"revision_number": 0}, "invalid_revision_number"),
            ({"revision_number": True}, "invalid_revision_number"),
            ({"revision_number": "1"}, "invalid_revision_number"),
            ({"revision_number": 1, "parent_revision_id": "rev-0"}, "invalid_lineage"),
            ({"revision_number": 2, "parent_revision_id": None}, "invalid_lineage"),
            ({"revision_jcs_sha256": "nothex"}, "invalid_digest"),
            ({"input_asset_pins": [object()]}, "invalid_asset_pins"),
            ({"input_asset_pins": [_asset_pin("a"), _asset_pin("a")]}, "invalid_asset_pins"),
        ],
    )
    def test_rejects_malformed_envelopes(self, overrides: dict[str, Any], reason: str) -> None:
        with pytest.raises(RevisionPinningError) as excinfo:
            self._envelope(**overrides)
        assert excinfo.value.reason == reason

    def test_non_iterable_pins_are_rejected(self) -> None:
        with pytest.raises(RevisionPinningError) as excinfo:
            self._envelope(input_asset_pins=7)
        assert excinfo.value.reason == "invalid_asset_pins"

    def test_tampered_envelope_fails_integrity(self) -> None:
        envelope = self._envelope()
        tampered = dataclasses.replace(envelope, preview_digest=f"sha256:{H2}")
        assert tampered.verify_envelope_integrity() is False
        relabeled = dataclasses.replace(envelope, revision_jcs_sha256=f"sha256:{'d' * 64}")
        assert relabeled.verify_envelope_integrity() is False

    def test_generation_two_envelope_with_parent_is_legal(self) -> None:
        envelope = self._envelope(revision_number=2, parent_revision_id="rev-1")
        assert envelope.verify_envelope_integrity() is True


# --------------------------------------------------------------------------- #
# Store: pinning, lineage, idempotency, concurrency
# --------------------------------------------------------------------------- #
class TestPinRevision:
    def test_genesis_pin_creates_the_head(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            envelope = store.pin_revision(revision_id="rev-1", **_pin_kwargs())
            assert envelope.revision_number == 1
            assert envelope.parent_revision_id is None
            assert store.head_of(PROJECT) == "rev-1"
            assert store.revision_status("rev-1") == PinnedRevisionState.LIVE
            assert store.get_envelope("rev-1") == envelope
            assert store.get_envelope("rev-unknown") is None
            assert store.head_of("proj-none") is None
            assert store.revision_status("rev-unknown") is None

    def test_linear_chain_one_two_three_marks_predecessors_superseded(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            first = store.pin_revision(revision_id="rev-1", **_pin_kwargs())
            second = store.pin_revision(
                revision_id="rev-2",
                **_pin_kwargs(revision_number=2, parent_revision_id="rev-1", plan_id="plan-2"),
            )
            third = store.pin_revision(
                revision_id="rev-3",
                **_pin_kwargs(revision_number=3, parent_revision_id="rev-2", plan_id="plan-3"),
            )
            assert store.head_of(PROJECT) == "rev-3"
            assert store.revision_status(first.revision_id) == "SUPERSEDED"
            assert store.revision_status(second.revision_id) == "SUPERSEDED"
            assert store.revision_status(third.revision_id) == "LIVE"
            assert [e.revision_id for e in store.list_revisions(PROJECT)] == [
                "rev-1",
                "rev-2",
                "rev-3",
            ]
            assert second.verify_envelope_integrity() is True
            assert third.revision_number == 3

    def test_two_turns_branching_off_the_same_parent_conflict(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            store.pin_revision(revision_id="rev-1", **_pin_kwargs())
            store.pin_revision(
                revision_id="rev-2a",
                **_pin_kwargs(revision_number=2, parent_revision_id="rev-1", turn_id="turn-a"),
            )
            with pytest.raises(RevisionConcurrencyConflictError) as excinfo:
                store.pin_revision(
                    revision_id="rev-2b",
                    **_pin_kwargs(revision_number=2, parent_revision_id="rev-1", turn_id="turn-b"),
                )
            assert excinfo.value.reason == "stale_parent"
            assert store.head_of(PROJECT) == "rev-2a"

    def test_genesis_on_an_existing_project_conflicts(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            store.pin_revision(revision_id="rev-1", **_pin_kwargs())
            with pytest.raises(RevisionConcurrencyConflictError) as excinfo:
                store.pin_revision(revision_id="rev-9", **_pin_kwargs())
            assert excinfo.value.reason == "genesis_on_existing_project"

    def test_revision_without_a_head_conflicts(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            with pytest.raises(RevisionConcurrencyConflictError) as excinfo:
                store.pin_revision(
                    revision_id="rev-2",
                    **_pin_kwargs(revision_number=2, parent_revision_id="rev-1"),
                )
            assert excinfo.value.reason == "missing_head"

    def test_revision_number_gap_conflicts(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            store.pin_revision(revision_id="rev-1", **_pin_kwargs())
            with pytest.raises(RevisionConcurrencyConflictError) as excinfo:
                store.pin_revision(
                    revision_id="rev-3",
                    **_pin_kwargs(revision_number=3, parent_revision_id="rev-1"),
                )
            assert excinfo.value.reason == "revision_number_gap"

    def test_cross_tenant_head_conflicts(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            store.pin_revision(revision_id="rev-1", **_pin_kwargs())
            with pytest.raises(RevisionConcurrencyConflictError) as excinfo:
                store.pin_revision(
                    revision_id="rev-2",
                    **_pin_kwargs(
                        revision_number=2,
                        parent_revision_id="rev-1",
                        tenant_id="tenant-other",
                    ),
                )
            assert excinfo.value.reason == "cross_tenant_head"

    def test_idempotent_retry_returns_the_stored_envelope(self, tmp_path: Path) -> None:
        clock = _Clock()
        with _store(tmp_path, now=clock) as store:
            kwargs = _pin_kwargs(input_asset_pins=[_asset_pin("b"), _asset_pin("a")])
            first = store.pin_revision(revision_id="rev-1", **kwargs)
            again = store.pin_revision(revision_id="rev-1", **kwargs)
            assert again == first
            assert again.created_at_utc == first.created_at_utc
            assert again.revision_jcs_sha256 == first.revision_jcs_sha256
            # f(f(x)) = f(x): a third call is still the first result.
            assert store.pin_revision(revision_id="rev-1", **kwargs) == first
            assert store.head_of(PROJECT) == "rev-1"

    def test_retry_after_reopen_is_still_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "pins.sqlite3"
        kwargs = _pin_kwargs()
        with DurableRevisionPinningStore(path) as store:
            first = store.pin_revision(revision_id="rev-1", **kwargs)
        with DurableRevisionPinningStore(path) as reopened:
            assert reopened.pin_revision(revision_id="rev-1", **kwargs) == first

    def test_conflicting_payload_under_the_same_id_is_rejected(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            store.pin_revision(revision_id="rev-1", **_pin_kwargs())
            for override in (
                {"plan_id": "plan-other"},
                {"preview_digest": H2},
                {"parameters_jcs_sha256": H3},
                {"input_asset_pins": [_asset_pin()]},
                {"turn_id": "turn-other"},
            ):
                with pytest.raises(RevisionIdempotencyConflictError) as excinfo:
                    store.pin_revision(revision_id="rev-1", **_pin_kwargs(**override))
                assert excinfo.value.reason == "idempotency_conflict"

    def test_audit_records_every_pin(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            store.pin_revision(revision_id="rev-1", **_pin_kwargs())
            store.pin_revision(
                revision_id="rev-2",
                **_pin_kwargs(revision_number=2, parent_revision_id="rev-1"),
            )
            events = store.audit_events()
            assert [event["event"] for event in events] == ["pinned", "pinned"]
            assert events[0]["detail"].startswith("sha256:")
            assert events[1]["revision_id"] == "rev-2"

    def test_wal_pragmas_and_foreign_keys(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert int(store._conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
            assert int(store._conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
            assert store.path == tmp_path / "pins.sqlite3"

    def test_close_is_idempotent_and_context_manager_closes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.close()
        store.close()
        with _store(tmp_path) as other:
            assert other.path.name == "pins.sqlite3"


# --------------------------------------------------------------------------- #
# Asset pinning from disk
# --------------------------------------------------------------------------- #
class TestPinAssetFromDisk:
    def test_pins_a_real_file_with_streamed_sha256_and_size(self, tmp_path: Path) -> None:
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"hello world")
        with _store(tmp_path) as store:
            pin = store.pin_asset_from_disk(
                asset_id="asset-intro",
                file_path=target,
                authorized_asset_root=root,
                media_type="video/mp4",
            )
        assert pin.relative_path == "media/intro.mp4"
        assert pin.byte_size == 11
        assert pin.content_sha256 == hashlib.sha256(b"hello world").hexdigest()
        assert pin.media_type == "video/mp4"

    def test_rejects_a_symlinked_file(self, tmp_path: Path) -> None:
        root = tmp_path / "assets"
        real = _write_asset(root, name="media/real.mp4")
        link = root / "media" / "link.mp4"
        link.symlink_to(real)
        with _store(tmp_path) as store:
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.pin_asset_from_disk(
                    asset_id="asset-link", file_path=link, authorized_asset_root=root
                )
        assert excinfo.value.reason == "symlink_rejected"

    def test_rejects_a_path_outside_the_authorized_root(self, tmp_path: Path) -> None:
        root = tmp_path / "assets"
        root.mkdir()
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"secret")
        with _store(tmp_path) as store:
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.pin_asset_from_disk(
                    asset_id="asset-escape", file_path=outside, authorized_asset_root=root
                )
        assert excinfo.value.reason == "path_escape"

    def test_rejects_a_missing_file_and_a_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "assets"
        root.mkdir()
        directory = root / "media"
        directory.mkdir()
        with _store(tmp_path) as store:
            with pytest.raises(AssetPinViolationError) as missing:
                store.pin_asset_from_disk(
                    asset_id="asset-x", file_path=root / "nope.mp4", authorized_asset_root=root
                )
            assert missing.value.reason == "asset_missing"
            with pytest.raises(AssetPinViolationError) as not_file:
                store.pin_asset_from_disk(
                    asset_id="asset-x", file_path=directory, authorized_asset_root=root
                )
            assert not_file.value.reason == "not_a_file"

    def test_rejects_a_blank_asset_id(self, tmp_path: Path) -> None:
        root = tmp_path / "assets"
        target = _write_asset(root)
        with _store(tmp_path) as store:
            with pytest.raises(RevisionPinningError):
                store.pin_asset_from_disk(
                    asset_id="  ", file_path=target, authorized_asset_root=root
                )

    def test_sha256_file_hex_streams_the_exact_bytes(self, tmp_path: Path) -> None:
        target = tmp_path / "big.bin"
        payload = os.urandom(3 * 1024 * 1024 + 7)
        target.write_bytes(payload)
        digest, size = sha256_file_hex(target)
        assert digest == hashlib.sha256(payload).hexdigest()
        assert size == len(payload)

    def test_utc_now_is_timezone_aware(self) -> None:
        assert utc_now().endswith("+00:00")


# --------------------------------------------------------------------------- #
# Worker verification: drift and pin violations
# --------------------------------------------------------------------------- #
class TestVerifyWorkerExecutionPins:
    def _pinned(
        self, tmp_path: Path
    ) -> tuple[DurableRevisionPinningStore, Path, PinnedRevisionEnvelope, AssetVersionPin]:
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"original-bytes")
        store = _store(tmp_path)
        pin = store.pin_asset_from_disk(
            asset_id="asset-a", file_path=target, authorized_asset_root=root
        )
        envelope = store.pin_revision(revision_id="rev-1", **_pin_kwargs(input_asset_pins=[pin]))
        return store, root, envelope, pin

    def test_ok_when_everything_matches(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            verified = store.verify_worker_execution_pins(
                revision_id="rev-1",
                expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                expected_plan_jcs_sha256=H,
                authorized_asset_root=root,
                expected_project_id=PROJECT,
                expected_tenant_id=TENANT,
            )
            assert verified == envelope
        finally:
            store.close()

    def test_unknown_revision_is_a_typed_not_found(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            with pytest.raises(RevisionNotFoundError):
                store.verify_worker_execution_pins(
                    revision_id="rev-missing",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
        finally:
            store.close()

    @pytest.mark.parametrize(
        "field,reason",
        [
            ("expected_project_id", "project_mismatch"),
            ("expected_tenant_id", "tenant_mismatch"),
        ],
    )
    def test_wrong_project_or_tenant_is_drift(
        self, tmp_path: Path, field: str, reason: str
    ) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            kwargs: dict[str, Any] = {
                "revision_id": "rev-1",
                "expected_revision_jcs_sha256": envelope.revision_jcs_sha256,
                "expected_plan_jcs_sha256": H,
                "authorized_asset_root": root,
            }
            kwargs[field] = "somewhere-else"
            with pytest.raises(RevisionDriftError) as excinfo:
                store.verify_worker_execution_pins(**kwargs)
            assert excinfo.value.reason == reason
        finally:
            store.close()

    def test_revision_digest_mismatch_is_drift(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            with pytest.raises(RevisionDriftError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=f"sha256:{'e' * 64}",
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
            assert excinfo.value.reason == "revision_drift"
        finally:
            store.close()

    def test_plan_digest_mismatch_is_drift(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            with pytest.raises(RevisionDriftError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H2,
                    authorized_asset_root=root,
                )
            assert excinfo.value.reason == "plan_drift"
        finally:
            store.close()

    def test_superseded_revision_is_refused_when_head_is_required(self, tmp_path: Path) -> None:
        store, root, first, _pin = self._pinned(tmp_path)
        try:
            second = store.pin_revision(
                revision_id="rev-2",
                **_pin_kwargs(revision_number=2, parent_revision_id="rev-1", input_asset_pins=[]),
            )
            with pytest.raises(RevisionDriftError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=first.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
            assert excinfo.value.reason == "not_current_head"
            # A historical replay is still verifiable when explicitly allowed.
            assert (
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=first.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                    require_current_head=False,
                )
                == first
            )
            assert store.head_of(PROJECT) == second.revision_id
        finally:
            store.close()

    def test_tampered_envelope_row_is_detected(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            store._conn.execute(
                "UPDATE nexus_studio_pinned_revisions SET preview_digest = ? WHERE revision_id = ?",
                (f"sha256:{'f' * 64}", "rev-1"),
            )
            with pytest.raises(RevisionDriftError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
            assert excinfo.value.reason == "envelope_tampered"
        finally:
            store.close()

    def test_modified_asset_bytes_are_detected(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            # Same byte length, different bytes: only the SHA-256 can catch this.
            (root / "media" / "intro.mp4").write_bytes(b"ORIGINAL-BYTES")
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
            assert excinfo.value.reason == "content_drift"
        finally:
            store.close()

    def test_size_mismatch_is_detected_before_hashing(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            (root / "media" / "intro.mp4").write_bytes(b"shorter")
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
            assert excinfo.value.reason == "size_mismatch"
        finally:
            store.close()

    def test_missing_asset_is_detected(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            (root / "media" / "intro.mp4").unlink()
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
            assert excinfo.value.reason == "asset_missing"
        finally:
            store.close()

    def test_asset_replaced_by_a_symlink_is_detected(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            real = root / "media" / "intro.mp4"
            payload = real.read_bytes()
            moved = root / "media" / "moved.mp4"
            moved.write_bytes(payload)
            real.unlink()
            real.symlink_to(moved)
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
            assert excinfo.value.reason == "symlink_rejected"
        finally:
            store.close()

    def test_asset_resolving_outside_the_root_is_detected(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            outside = tmp_path / "outside.mp4"
            payload = (root / "media" / "intro.mp4").read_bytes()
            outside.write_bytes(payload)
            link_dir = root / "media"
            (link_dir / "intro.mp4").unlink()
            # A *directory* symlink is not followed by lstat on the file again,
            # so simulate the escape with a hard link-free bind: a relative pin
            # that resolves outside via a symlinked parent directory.
            (link_dir / "intro.mp4").symlink_to(outside)
            with pytest.raises(AssetPinViolationError):
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
        finally:
            store.close()

    def test_asset_that_is_a_directory_is_detected(self, tmp_path: Path) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            target = root / "media" / "intro.mp4"
            target.unlink()
            target.mkdir()
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
            assert excinfo.value.reason == "not_a_file"
        finally:
            store.close()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("revision_id", ""),
            ("revision_id", "   "),
            ("expected_revision_jcs_sha256", "nope"),
            ("expected_plan_jcs_sha256", "sha256:zz"),
        ],
    )
    def test_malformed_verification_arguments_are_refused(
        self, tmp_path: Path, field: str, value: str
    ) -> None:
        store, root, envelope, _pin = self._pinned(tmp_path)
        try:
            kwargs: dict[str, Any] = {
                "revision_id": "rev-1",
                "expected_revision_jcs_sha256": envelope.revision_jcs_sha256,
                "expected_plan_jcs_sha256": H,
                "authorized_asset_root": root,
            }
            kwargs[field] = value
            with pytest.raises(RevisionPinningError):
                store.verify_worker_execution_pins(**kwargs)
        finally:
            store.close()


# --------------------------------------------------------------------------- #
# Closed-state guard + schema durability
# --------------------------------------------------------------------------- #
def test_pinned_revision_state_is_a_closed_set() -> None:
    assert PinnedRevisionState("LIVE") == "LIVE"
    assert PinnedRevisionState("SUPERSEDED") == "SUPERSEDED"
    with pytest.raises(RevisionPinningError):
        PinnedRevisionState("ARCHIVED")


def test_envelope_json_round_trips_through_the_store(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    target = _write_asset(root, payload=b"round-trip")
    with _store(tmp_path) as store:
        pin = store.pin_asset_from_disk(
            asset_id="asset-z", file_path=target, authorized_asset_root=root
        )
        envelope = store.pin_revision(revision_id="rev-1", **_pin_kwargs(input_asset_pins=[pin]))
        raw = store._conn.execute(
            "SELECT input_asset_pins_json FROM nexus_studio_pinned_revisions "
            "WHERE revision_id = 'rev-1'"
        ).fetchone()[0]
        assert json.loads(raw)[0]["asset_id"] == "asset-z"
        reloaded = store.get_envelope("rev-1")
        assert reloaded == envelope
        assert reloaded is not None
        assert reloaded.input_asset_pins[0].media_type == "application/octet-stream"


def test_store_is_readable_by_a_second_connection(tmp_path: Path) -> None:
    path = tmp_path / "pins.sqlite3"
    with _store(tmp_path) as store:
        envelope = store.pin_revision(revision_id="rev-1", **_pin_kwargs())
    raw = sqlite3.connect(str(path))
    try:
        row = raw.execute(
            "SELECT revision_id, revision_jcs_sha256 FROM nexus_studio_pinned_revisions"
        ).fetchone()
        assert row == ("rev-1", envelope.revision_jcs_sha256)
    finally:
        raw.close()


class TestFailClosedTransactionsAndEscapes:
    def test_a_raising_write_rolls_back_and_leaves_no_row(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            with pytest.raises(RuntimeError):
                with store._transaction():
                    store._conn.execute(
                        "INSERT INTO nexus_studio_pinned_revisions (revision_id, project_id, "
                        "tenant_id, session_id, turn_id, revision_number, plan_id, "
                        "plan_jcs_sha256, parameters_jcs_sha256, preview_digest, "
                        "input_asset_pins_json, revision_jcs_sha256, status, created_at_utc) "
                        "VALUES ('rev-x', 'p', 't', 's', 'tu', 1, 'pl', ?, ?, ?, '[]', ?, "
                        "'LIVE', 'now')",
                        (H, H, H, H),
                    )
                    raise RuntimeError("boom")
            assert store.get_envelope("rev-x") is None

    def test_nested_transactions_inside_the_store_join_the_outer_one(self, tmp_path: Path) -> None:
        """A nested ``with store._transaction()`` must not open a second BEGIN."""
        with _store(tmp_path) as store:
            with store._transaction():
                with store._transaction():
                    store._conn.execute(
                        "INSERT INTO nexus_studio_pinned_revisions (revision_id, project_id, "
                        "tenant_id, session_id, turn_id, revision_number, plan_id, "
                        "plan_jcs_sha256, parameters_jcs_sha256, preview_digest, "
                        "input_asset_pins_json, revision_jcs_sha256, status, created_at_utc) "
                        "VALUES ('rev-nested', 'p', 't', 's', 'tu', 1, 'pl', ?, ?, ?, '[]', ?, "
                        "'LIVE', 'now')",
                        (H, H, H, H),
                    )
            assert store.get_envelope("rev-nested") is not None

    def test_nested_read_inside_a_write_transaction_joins_it(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            with store._transaction():
                assert store.get_envelope("rev-missing") is None
                assert store.head_of(PROJECT) is None

    def test_asset_symlink_target_outside_the_root_is_a_path_escape(self, tmp_path: Path) -> None:
        """A symlink is refused by lstat before resolve can follow it out."""
        root = tmp_path / "assets"
        root.mkdir()
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"escape")
        link = root / "escape.mp4"
        link.symlink_to(outside)
        with _store(tmp_path) as store:
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.pin_asset_from_disk(
                    asset_id="asset-escape", file_path=link, authorized_asset_root=root
                )
        assert excinfo.value.reason == "symlink_rejected"

    def test_pinned_row_whose_parent_directory_escapes_is_a_path_escape(
        self, tmp_path: Path
    ) -> None:
        """A row pinning through a symlinked *directory* out of the root is refused.

        ``AssetVersionPin`` refuses a traversing ``relative_path`` at
        construction, so the only remaining escape is a path that is *spelled*
        relative yet resolves outside the root through a symlinked parent.
        """
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"payload")
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "evil.mp4").write_bytes(b"payload")
        with _store(tmp_path) as store:
            pin = store.pin_asset_from_disk(
                asset_id="asset-a", file_path=target, authorized_asset_root=root
            )
            envelope = store.pin_revision(
                revision_id="rev-1", **_pin_kwargs(input_asset_pins=[pin])
            )
            escaped = AssetVersionPin(
                asset_id=pin.asset_id,
                relative_path="linked/evil.mp4",
                content_sha256=pin.content_sha256,
                byte_size=pin.byte_size,
            )
            (root / "linked").symlink_to(outside_dir)
            forged = dataclasses.replace(
                envelope, input_asset_pins=(escaped,), revision_jcs_sha256=""
            )
            store._conn.execute(
                "UPDATE nexus_studio_pinned_revisions SET input_asset_pins_json = ?, "
                "revision_jcs_sha256 = ? WHERE revision_id = 'rev-1'",
                (
                    json.dumps([escaped.to_canonical_dict()], sort_keys=True),
                    forged.revision_jcs_sha256,
                ),
            )
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=forged.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
        assert excinfo.value.reason == "path_escape"

    def test_pinned_row_pointing_at_an_unknown_path_is_a_path_escape(self, tmp_path: Path) -> None:
        """A pin whose row was edited to a non-existent sub-path refuses as missing."""
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"payload")
        with _store(tmp_path) as store:
            pin = store.pin_asset_from_disk(
                asset_id="asset-a", file_path=target, authorized_asset_root=root
            )
            envelope = store.pin_revision(
                revision_id="rev-1", **_pin_kwargs(input_asset_pins=[pin])
            )
            moved = AssetVersionPin(
                asset_id=pin.asset_id,
                relative_path="media/gone.mp4",
                content_sha256=pin.content_sha256,
                byte_size=pin.byte_size,
            )
            forged = dataclasses.replace(
                envelope, input_asset_pins=(moved,), revision_jcs_sha256=""
            )
            store._conn.execute(
                "UPDATE nexus_studio_pinned_revisions SET input_asset_pins_json = ?, "
                "revision_jcs_sha256 = ? WHERE revision_id = 'rev-1'",
                (
                    json.dumps([moved.to_canonical_dict()], sort_keys=True),
                    forged.revision_jcs_sha256,
                ),
            )
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=forged.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
        assert excinfo.value.reason == "asset_missing"

    def test_unicode_paths_digest_stably(self, tmp_path: Path) -> None:
        root = tmp_path / "assets"
        target = _write_asset(root, name="media/مقدمه.mp4", payload=b"persian")
        with _store(tmp_path) as store:
            pin = store.pin_asset_from_disk(
                asset_id="asset-fa", file_path=target, authorized_asset_root=root
            )
            envelope = store.pin_revision(
                revision_id="rev-1", **_pin_kwargs(input_asset_pins=[pin])
            )
            assert (
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=envelope.revision_jcs_sha256,
                    expected_plan_jcs_sha256=H,
                    authorized_asset_root=root,
                )
                == envelope
            )


def test_store_rejects_unknown_message_shape(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        with pytest.raises(RevisionPinningError):
            store.pin_revision(revision_id="rev-1", **_pin_kwargs(revision_number=0))


# --------------------------------------------------------------------------- #
# Wave 3 — TOCTOU-safe descriptor pinning and real relational integrity
# --------------------------------------------------------------------------- #
class TestDescriptorPinningClosesTheToctouvWindow:
    """``lstat`` then ``open`` is a gap; one descriptor is not.

    Each test replaces the file *between* the inspection and the read, which is
    exactly the substitution an attacker on a shared filesystem performs.
    """

    def test_inode_substitution_between_lstat_and_open_is_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"approved-bytes")
        with _store(tmp_path) as store:
            real_lstat = os.lstat
            swapped: dict[str, Any] = {}

            def swapping_lstat(path: Any) -> os.stat_result:
                result = real_lstat(path)
                if str(path).endswith("intro.mp4") and "done" not in swapped:
                    # Swap the *content* and the inode after the inspection the
                    # store is about to trust.
                    replacement = target.with_name("intro.mp4.swapped")
                    replacement.write_bytes(b"EVIL-REPLACEMENT-BYTES")
                    os.replace(replacement, target)
                    swapped["done"] = True
                return result

            monkeypatch.setattr(os, "lstat", swapping_lstat)
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.pin_asset_from_disk(
                    asset_id="asset-intro", file_path=target, authorized_asset_root=root
                )
        assert excinfo.value.reason == "inode_substituted"
        assert swapped["done"] is True

    def test_a_pin_verification_also_refuses_a_substituted_inode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"pinned-bytes")
        with _store(tmp_path) as store:
            pin = store.pin_asset_from_disk(
                asset_id="asset-intro", file_path=target, authorized_asset_root=root
            )
            stored = store.pin_revision(revision_id="rev-1", **_pin_kwargs(input_asset_pins=(pin,)))
            real_lstat = os.lstat
            state: dict[str, Any] = {}

            def swapping_lstat(path: Any) -> os.stat_result:
                result = real_lstat(path)
                if str(path).endswith("intro.mp4") and "done" not in state:
                    # Same size, different inode: only the (dev, ino) pair catches
                    # this, which is why the check is not merely a size compare.
                    replacement = target.with_name("intro.mp4.evil")
                    replacement.write_bytes(b"pinned-byteZ")
                    os.replace(replacement, target)
                    state["done"] = True
                return result

            monkeypatch.setattr(os, "lstat", swapping_lstat)
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.verify_worker_execution_pins(
                    revision_id="rev-1",
                    expected_revision_jcs_sha256=stored.revision_jcs_sha256,
                    expected_plan_jcs_sha256=stored.plan_jcs_sha256,
                    authorized_asset_root=root,
                    expected_project_id=PROJECT,
                    expected_tenant_id=TENANT,
                )
        assert excinfo.value.reason == "inode_substituted"

    def test_reading_through_the_descriptor_never_reopens_the_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"descriptor-only")
        opened: list[str] = []
        real_open = os.open

        def tracking_open(path: Any, *args: Any, **kwargs: Any) -> int:
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", tracking_open)
        with _store(tmp_path) as store:
            pin = store.pin_asset_from_disk(
                asset_id="asset-intro", file_path=target, authorized_asset_root=root
            )
        assert pin.content_sha256 == hashlib.sha256(b"descriptor-only").hexdigest()
        assert str(target) in opened
        assert all("intro.mp4" in entry for entry in opened)

    def test_a_fifo_or_device_is_refused_through_the_descriptor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"regular")
        with _store(tmp_path) as store:
            # Force ``fstat`` to describe a non-regular file, as a FIFO or device
            # would, without needing privileges to create one.
            real_fstat = os.fstat

            def lying_fstat(fd: int) -> os.stat_result:
                result = real_fstat(fd)
                fields = list(result)
                fields[0] = (result.st_mode & ~0o170000) | 0o010000  # S_IFIFO
                return os.stat_result(fields)

            monkeypatch.setattr(os, "fstat", lying_fstat)
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.pin_asset_from_disk(
                    asset_id="asset-fifo", file_path=target, authorized_asset_root=root
                )
        assert excinfo.value.reason == "not_a_file"

    def test_a_file_that_grows_while_being_read_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"short")
        with _store(tmp_path) as store:
            real_fstat = os.fstat

            def lying_fstat(fd: int) -> os.stat_result:
                result = real_fstat(fd)
                fields = list(result)
                fields[6] = result.st_size + 100  # st_size disagrees with the read
                return os.stat_result(fields)

            monkeypatch.setattr(os, "fstat", lying_fstat)
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.pin_asset_from_disk(
                    asset_id="asset-grow", file_path=target, authorized_asset_root=root
                )
        assert excinfo.value.reason == "inode_substituted" or (
            excinfo.value.reason == "size_mismatch"
        )

    def test_a_dangling_symlink_at_open_time_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "assets"
        (root / "media").mkdir(parents=True)
        target = root / "media" / "vanishing.mp4"
        target.write_bytes(b"payload")
        with _store(tmp_path) as store:
            target.unlink()
            target.symlink_to(tmp_path / "nowhere")
            with pytest.raises(AssetPinViolationError) as excinfo:
                store.pin_asset_from_disk(
                    asset_id="asset-vanish", file_path=target, authorized_asset_root=root
                )
        assert excinfo.value.reason in {"symlink_rejected", "asset_open_failed"}


class TestRelationalIntegrityIsEnforcedBySqlite:
    def test_pragmas_and_the_composite_key_are_present(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            assert store._conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
            keys = store._conn.execute(
                "PRAGMA index_list(nexus_studio_pinned_revisions)"
            ).fetchall()
            assert any(row[2] == 1 for row in keys)  # at least one UNIQUE index

    def test_an_orphan_asset_pin_row_is_rejected_by_the_foreign_key(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            with pytest.raises(sqlite3.IntegrityError) as excinfo:
                store._conn.execute(
                    "INSERT INTO nexus_studio_revision_pin_audit "
                    "(revision_id, project_id, event, detail, occurred_at_utc) "
                    "VALUES ('ghost', 'proj', 'pin', 'orphan', 'now')"
                )
        assert "FOREIGN KEY" in str(excinfo.value)

    def test_a_real_audit_row_is_accepted_and_protected_from_deletion(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            store.pin_revision(revision_id="rev-1", **_pin_kwargs())
            rows = store.audit_events()
            assert rows, "the pin must have written audit history"
            with pytest.raises(sqlite3.IntegrityError) as excinfo:
                store._conn.execute("DELETE FROM nexus_studio_pinned_revisions")
        assert "FOREIGN KEY" in str(excinfo.value)

    def test_a_head_may_not_name_a_revision_that_does_not_exist(self, tmp_path: Path) -> None:
        with _store(tmp_path) as store:
            with pytest.raises(sqlite3.IntegrityError) as excinfo:
                store._conn.execute(
                    "INSERT INTO nexus_studio_project_revision_heads "
                    "(project_id, tenant_id, head_revision_id, head_revision_number, "
                    "updated_at_utc) VALUES ('proj', 'tenant', 'ghost', 1, 'now')"
                )
        assert "head_reference_missing" in str(excinfo.value)

    def test_an_open_that_fails_is_a_typed_refusal_not_a_crash(self, tmp_path: Path) -> None:
        """O_NOFOLLOW turns a swapped-in symlink into a plain open failure."""
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"payload")
        with _store(tmp_path):
            inspected = os.lstat(target)
            os.unlink(target)
            target.symlink_to(tmp_path / "definitely-missing")
            with pytest.raises(AssetPinViolationError) as excinfo:
                sha256_pinned_file(target, expected=inspected, authorized_root=root)
        assert excinfo.value.reason == "asset_open_failed"

    def test_the_descriptor_path_matches_sha256_file_hex_on_a_clean_file(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "assets"
        target = _write_asset(root, payload=b"agrees")
        inspected = os.lstat(target)
        assert sha256_pinned_file(target, expected=inspected, authorized_root=root) == (
            sha256_file_hex(target)
        )
        assert root.is_dir()
