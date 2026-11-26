"""Wave 3 slice 3.3 — durable, revocable privacy consent.

Pins the ledger contract on real disk:

* consent is bound to ``(principal_id, project_id, provider_id, data_category)``
  *and* to the processing action (local vs external upload);
* windows are time-bounded and an expired grant never authorizes;
* revocation is immediate, idempotent and recorded in an append-only,
  hash-chained audit log;
* the audit log and the grant table never carry a raw sensitive payload;
* the fail-closed gate raises a typed ``PrivacyViolation`` for every reason;
* the IBPC service invokes the gate before local execution and refuses when a
  grant is missing or revoked between preview and execution.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus_ai_agent.nagar.ibpc.privacy import (
    DEFAULT_CONSENT_REASON,
    ConsentAction,
    ConsentScope,
    ConsentState,
    ConsentStoreError,
    DurablePrivacyConsentStore,
    PrivacyMode,
    PrivacyPolicy,
    PrivacyViolation,
    durable_privacy_consent_path,
)

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
TOMORROW = "2026-11-26T00:00:00Z"
PRINCIPAL = "prn_" + "1" * 32
PROJECT = "project-w3"
LOCAL = "local"
REMOTE = "provider-remote-1"


@pytest.fixture()
def store(tmp_path: Path):
    handle = DurablePrivacyConsentStore(tmp_path / "consent.sqlite3", now=NOW)
    yield handle
    handle.close()


def _grant(store: DurablePrivacyConsentStore, **overrides):
    params = dict(
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        provider_id=LOCAL,
        data_category=ConsentScope.AUDIO,
        action=ConsentAction.LOCAL_PROCESSING,
        expires_at_utc=TOMORROW,
    )
    params.update(overrides)
    return store.grant_consent(**params)  # type: ignore[arg-type]


def _evaluate(store: DurablePrivacyConsentStore, **overrides):
    params = dict(
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        provider_id=LOCAL,
        data_category=ConsentScope.AUDIO,
        action=ConsentAction.LOCAL_PROCESSING,
    )
    params.update(overrides)
    return store.evaluate_processing_consent(**params)  # type: ignore[arg-type]


class TestConsentScopeVocabulary:
    def test_scopes_and_actions_are_closed_literal_sets(self) -> None:
        assert {scope.value for scope in ConsentScope} == {
            "AUDIO",
            "VIDEO",
            "IMAGE",
            "TRANSCRIPT",
            "BIOMETRIC_FACE_ROI",
        }
        assert {action.value for action in ConsentAction} == {
            "LOCAL_PROCESSING",
            "EXTERNAL_PROVIDER_UPLOAD",
        }

    def test_canonical_path_is_under_the_queue_directory(self, tmp_path: Path) -> None:
        expected = tmp_path / "queue" / "ibpc_privacy_consent.sqlite3"
        assert durable_privacy_consent_path(tmp_path) == expected


class TestGrantAndGate:
    def test_grant_allows_the_exact_processing_step(self, store) -> None:
        granted = _grant(store)
        assert granted.state is ConsentState.GRANTED
        assert granted.reason == DEFAULT_CONSENT_REASON
        live = _evaluate(store)
        assert live.consent_id == granted.consent_id

    def test_missing_consent_fails_closed(self, store) -> None:
        with pytest.raises(PrivacyViolation) as exc:
            _evaluate(store)
        assert exc.value.reason == "consent_missing"

    def test_a_grant_is_bound_to_provider_category_and_action(self, store) -> None:
        _grant(store)
        for override in (
            {"provider_id": REMOTE},
            {"data_category": ConsentScope.VIDEO},
            {"action": ConsentAction.EXTERNAL_PROVIDER_UPLOAD},
            {"project_id": "other-project"},
            {"principal_id": "prn_" + "2" * 32},
        ):
            with pytest.raises(PrivacyViolation) as exc:
                _evaluate(store, **override)
            assert exc.value.reason == "consent_missing"

    def test_every_media_category_can_be_granted_independently(self, store) -> None:
        for category in ConsentScope:
            _grant(store, data_category=category, provider_id=f"p-{category.value}")
        for category in ConsentScope:
            assert (
                _evaluate(store, data_category=category, provider_id=f"p-{category.value}").state
                is ConsentState.GRANTED
            )

    def test_external_upload_consent_is_a_separate_grant(self, store) -> None:
        _grant(
            store,
            provider_id=REMOTE,
            data_category=ConsentScope.BIOMETRIC_FACE_ROI,
            action=ConsentAction.EXTERNAL_PROVIDER_UPLOAD,
        )
        assert (
            _evaluate(
                store,
                provider_id=REMOTE,
                data_category=ConsentScope.BIOMETRIC_FACE_ROI,
                action=ConsentAction.EXTERNAL_PROVIDER_UPLOAD,
            ).action
            is ConsentAction.EXTERNAL_PROVIDER_UPLOAD
        )

    def test_re_affirming_the_identical_grant_is_idempotent(self, store) -> None:
        first = _grant(store)
        again = _grant(store)
        assert again.consent_id == first.consent_id

    def test_re_granting_with_a_different_expiry_is_refused(self, store) -> None:
        _grant(store)
        with pytest.raises(ConsentStoreError) as exc:
            _grant(store, expires_at_utc="2026-12-01T00:00:00Z")
        assert exc.value.reason == "duplicate_grant"

    def test_expiry_must_be_in_the_future(self, store) -> None:
        with pytest.raises(ConsentStoreError) as exc:
            _grant(store, expires_at_utc=NOW())
        assert exc.value.reason == "invalid_window"

    def test_unparsable_expiry_is_refused(self, store) -> None:
        with pytest.raises(ValueError):
            _grant(store, expires_at_utc="tomorrow")

    @pytest.mark.parametrize(
        "overrides,reason",
        [
            ({"data_category": "AUDIO"}, "invalid_category"),
            ({"action": "LOCAL_PROCESSING"}, "invalid_action"),
            ({"principal_id": ""}, "invalid_grant"),
            ({"project_id": ""}, "invalid_grant"),
            ({"provider_id": ""}, "invalid_grant"),
            ({"reason": ""}, "invalid_grant"),
            ({"reason": "x" * 300}, "invalid_grant"),
        ],
    )
    def test_malformed_grants_are_typed_refusals(self, store, overrides, reason) -> None:
        with pytest.raises(ConsentStoreError) as exc:
            _grant(store, **overrides)
        assert exc.value.reason == reason

    @pytest.mark.parametrize(
        "overrides,reason",
        [
            ({"data_category": "AUDIO"}, "invalid_category"),
            ({"action": "LOCAL_PROCESSING"}, "invalid_action"),
        ],
    )
    def test_the_gate_validates_its_arguments(self, store, overrides, reason) -> None:
        with pytest.raises(ConsentStoreError) as exc:
            _evaluate(store, **overrides)
        assert exc.value.reason == reason


class TestTimeBoundedConsent:
    def test_an_expired_grant_never_authorizes(self, tmp_path: Path) -> None:
        store = DurablePrivacyConsentStore(tmp_path / "expiry.sqlite3", now=NOW)
        try:
            _grant(store, expires_at_utc="2026-11-25T00:30:00Z")
            assert _evaluate(store).state is ConsentState.GRANTED
            store._now = lambda: "2026-11-25T00:30:00Z"
            with pytest.raises(PrivacyViolation) as exc:
                _evaluate(store)
            assert exc.value.reason == "consent_expired"
        finally:
            store.close()

    def test_expiry_is_evaluated_per_read_not_per_write(self, tmp_path: Path) -> None:
        clock = {"now": "2026-11-25T00:00:00Z"}
        store = DurablePrivacyConsentStore(tmp_path / "clock.sqlite3", now=lambda: clock["now"])
        try:
            _grant(store, expires_at_utc="2026-11-25T01:00:00Z")
            clock["now"] = "2026-11-25T00:59:59Z"
            _evaluate(store)
            clock["now"] = "2026-11-25T01:00:01Z"
            with pytest.raises(PrivacyViolation):
                _evaluate(store)
        finally:
            store.close()


class TestRevocation:
    def test_revocation_is_immediate(self, store) -> None:
        _grant(store)
        revoked = store.revoke_consent(
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            provider_id=LOCAL,
            data_category=ConsentScope.AUDIO,
            action=ConsentAction.LOCAL_PROCESSING,
        )
        assert revoked.state is ConsentState.REVOKED
        assert revoked.revoked_at_utc == NOW()
        with pytest.raises(PrivacyViolation) as exc:
            _evaluate(store)
        assert exc.value.reason == "consent_revoked"

    def test_revocation_is_idempotent(self, store) -> None:
        _grant(store)
        params = dict(
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            provider_id=LOCAL,
            data_category=ConsentScope.AUDIO,
            action=ConsentAction.LOCAL_PROCESSING,
        )
        first = store.revoke_consent(**params)
        second = store.revoke_consent(**params)
        assert first == second
        assert [e["event"] for e in store.audit_log()] == ["GRANTED", "REVOKED"]

    def test_revoking_an_absent_grant_is_a_typed_refusal(self, store) -> None:
        with pytest.raises(ConsentStoreError) as exc:
            store.revoke_consent(
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provider_id=LOCAL,
                data_category=ConsentScope.AUDIO,
                action=ConsentAction.LOCAL_PROCESSING,
            )
        assert exc.value.reason == "unknown_consent"

    def test_revocation_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "restart.sqlite3"
        first = DurablePrivacyConsentStore(path, now=NOW)
        _grant(first)
        first.revoke_consent(
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            provider_id=LOCAL,
            data_category=ConsentScope.AUDIO,
            action=ConsentAction.LOCAL_PROCESSING,
        )
        first.close()
        second = DurablePrivacyConsentStore(path, now=NOW)
        try:
            with pytest.raises(PrivacyViolation) as exc:
                _evaluate(second)
            assert exc.value.reason == "consent_revoked"
        finally:
            second.close()

    def test_regranting_after_revocation_works_and_is_audited(self, store) -> None:
        _grant(store)
        store.revoke_consent(
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            provider_id=LOCAL,
            data_category=ConsentScope.AUDIO,
            action=ConsentAction.LOCAL_PROCESSING,
        )
        again = _grant(store)
        assert again.state is ConsentState.GRANTED
        assert again.revoked_at_utc is None
        assert [e["event"] for e in store.audit_log()] == ["GRANTED", "REVOKED", "GRANTED"]

    def test_revoke_all_for_a_principal_covers_every_grant(self, store) -> None:
        _grant(store, data_category=ConsentScope.AUDIO)
        _grant(store, data_category=ConsentScope.VIDEO, provider_id="second")
        other = "prn_" + "9" * 32
        _grant(store, principal_id=other, provider_id="third")
        revoked = store.revoke_all_for_principal(PRINCIPAL)
        assert len(revoked) == 2
        assert {r.state for r in revoked} == {ConsentState.REVOKED}
        # another principal's grant is untouched
        assert (
            store.get_consent(
                principal_id=other,
                project_id=PROJECT,
                provider_id="third",
                data_category=ConsentScope.AUDIO,
                action=ConsentAction.LOCAL_PROCESSING,
            ).state
            is ConsentState.GRANTED
        )


class TestAuditLog:
    def test_audit_log_is_append_only_and_hash_chained(self, store) -> None:
        _grant(store)
        store.revoke_consent(
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            provider_id=LOCAL,
            data_category=ConsentScope.AUDIO,
            action=ConsentAction.LOCAL_PROCESSING,
        )
        log = store.audit_log()
        assert [e["event"] for e in log] == ["GRANTED", "REVOKED"]
        assert log[0]["previous_sha256"] == ""
        assert log[1]["previous_sha256"] == log[0]["event_sha256"]
        assert store.verify_audit_chain() is True

    def test_editing_an_audit_row_breaks_the_chain(self, store) -> None:
        _grant(store)
        store._conn.execute(
            "UPDATE privacy_consent_audit_log SET event = 'REVOKED' WHERE event_id = 1"
        )
        assert store.verify_audit_chain() is False

    def test_deleting_an_audit_row_breaks_the_chain(self, store) -> None:
        _grant(store)
        _grant(store, data_category=ConsentScope.VIDEO)
        store._conn.execute("DELETE FROM privacy_consent_audit_log WHERE event_id = 1")
        assert store.verify_audit_chain() is False

    def test_no_raw_sensitive_payload_is_ever_stored(self, store, tmp_path: Path) -> None:
        secret = "raw audio bytes must never be persisted / transcript words"
        _grant(store, reason="operator reviewed the request")
        assert secret not in store.path.read_bytes().decode("utf-8", "ignore")
        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw = store.path.read_bytes()
        assert secret.encode() not in raw
        for row in store.audit_log():
            assert set(row) == {
                "event_id",
                "consent_id",
                "principal_id",
                "project_id",
                "provider_id",
                "data_category",
                "action",
                "event",
                "recorded_at_utc",
                "previous_sha256",
                "event_sha256",
            }

    def test_audit_events_are_constrained_by_the_schema(self, store) -> None:
        _grant(store)
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO privacy_consent_audit_log (consent_id, principal_id, project_id,"
                " provider_id, data_category, action, event, recorded_at_utc, previous_sha256,"
                " event_sha256) VALUES ('c','p','pr','prov','AUDIO','LOCAL_PROCESSING',"
                " 'FORGED','now','', 'digest')"
            )


class TestDurability:
    def test_grants_survive_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "durable.sqlite3"
        first = DurablePrivacyConsentStore(path, now=NOW)
        granted = _grant(first)
        first.close()
        second = DurablePrivacyConsentStore(path, now=NOW)
        try:
            restored = second.get_consent(
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provider_id=LOCAL,
                data_category=ConsentScope.AUDIO,
                action=ConsentAction.LOCAL_PROCESSING,
            )
            assert restored == granted
            assert _evaluate(second).consent_id == granted.consent_id
            assert second.verify_audit_chain() is True
        finally:
            second.close()

    def test_unknown_consent_read_is_typed(self, store) -> None:
        with pytest.raises(ConsentStoreError) as exc:
            store.get_consent(
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provider_id=LOCAL,
                data_category=ConsentScope.AUDIO,
                action=ConsentAction.LOCAL_PROCESSING,
            )
        assert exc.value.reason == "unknown_consent"

    def test_the_file_is_private(self, tmp_path: Path) -> None:
        path = tmp_path / "private.sqlite3"
        store = DurablePrivacyConsentStore(path, now=NOW)
        try:
            assert (path.stat().st_mode & 0o077) == 0
        finally:
            store.close()


class TestInMemoryPolicySeams:
    """The in-memory policy is the first gate; its type checks stay fail-closed."""

    def test_the_consent_log_is_exposed_read_only(self) -> None:
        policy = PrivacyPolicy(now=NOW)
        assert policy.consents == ()
        policy.record_consent(PrivacyMode.TRUSTED_CLOUD, principal_id=PRINCIPAL, scope="audio")
        assert [record.scope for record in policy.consents] == ["audio"]

    def test_local_processing_refuses_an_unknown_mode(self) -> None:
        policy = PrivacyPolicy(now=NOW)
        with pytest.raises(PrivacyViolation) as exc:
            policy.require_local("local_vault", "video")  # type: ignore[arg-type]
        assert exc.value.reason == "invalid_mode"

    def test_record_consent_refuses_an_unknown_mode(self) -> None:
        policy = PrivacyPolicy(now=NOW)
        with pytest.raises(PrivacyViolation) as exc:
            policy.record_consent("cloud", principal_id=PRINCIPAL, scope="audio")  # type: ignore[arg-type]
        assert exc.value.reason == "invalid_mode"
        assert policy.consents == ()


class TestDurableTimestampValidation:
    def test_a_naive_instant_is_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.privacy import _parse_instant

        with pytest.raises(ConsentStoreError) as exc:
            _parse_instant("2026-11-25T00:00:00")
        assert exc.value.reason == "invalid_timestamp"

    def test_a_naive_stored_window_is_refused(self, tmp_path: Path) -> None:
        store = DurablePrivacyConsentStore(tmp_path / "consent.sqlite3", now=NOW)
        try:
            grant = store.grant_consent(
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provider_id="local",
                data_category=ConsentScope.AUDIO,
                action=ConsentAction.LOCAL_PROCESSING,
                expires_at_utc="2027-01-01T00:00:00Z",
            )
            store._conn.execute(
                "UPDATE privacy_consents SET expires_at_utc = ? WHERE consent_id = ?",
                ("2027-01-01T00:00:00", grant.consent_id),
            )
            with pytest.raises(ConsentStoreError) as exc:
                store.evaluate_processing_consent(
                    principal_id=PRINCIPAL,
                    project_id=PROJECT,
                    provider_id="local",
                    data_category=ConsentScope.AUDIO,
                    action=ConsentAction.LOCAL_PROCESSING,
                )
            assert exc.value.reason in {"invalid_timestamp", "consent_expired"}
        finally:
            store.close()
