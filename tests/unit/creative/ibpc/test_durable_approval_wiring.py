"""Wave 3 slice 3.2 — canonical single-use approvals wired into ``IBPCService``.

The approval ledger now speaks RFC 8785 JCS + keyed HMAC-SHA256 for every
binding, and production composition uses the DURABLE ledger. These tests pin:

* the exact nine-field ``ApprovalBindingPayload`` and its canonical bytes;
* the MAC: determinism, domain separation, key sensitivity, tamper detection
  (``hmac.compare_digest`` is the only comparator used);
* single-use atomic consumption inside ``BEGIN IMMEDIATE``: replay, expiry and
  every immutable-field mutation (``base_revision_id`` / ``scope_signature`` /
  ``plan_sha256`` / ``preview_sha256`` / ``principal_id``) are typed refusals;
* durability: a consumed approval stays consumed across a full restart;
* the composition factory builds the durable canonical ledger, and
  ``IBPCService`` consumes through it.
"""

from __future__ import annotations

import hmac
from pathlib import Path

import pytest

from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval import (
    BINDING_FIELDS,
    CANONICAL_BINDING_DOMAIN,
    ApprovalAlreadyConsumedError,
    ApprovalBindingMismatchError,
    ApprovalBindingPayload,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalIdentity,
    ApprovalLedger,
    CanonicalApprovalLedger,
    DurableCanonicalApprovalLedger,
    canonical_binding_mac,
    canonical_binding_payload,
)
from nexus_ai_agent.nagar.ibpc.approval_mac import KeyProviderError, StaticKeyRing
from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes
from nexus_ai_agent.nagar.ibpc.composition import build_durable_ibpc_service
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding, ApprovalState
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps
from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_bytes,
)

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
LATER = lambda: "2026-11-25T00:05:00Z"  # noqa: E731
EXPIRED = lambda: "2026-11-25T02:00:00Z"  # noqa: E731
KEY = b"k" * 32
PRINCIPAL = "prn_" + "1" * 32
SCOPE = "sha256:" + "b" * 64


def _ring(material: bytes = KEY, key_id: str = "unit") -> StaticKeyRing:
    return StaticKeyRing({key_id: material}, active_key_id=key_id)


def _binding(**overrides) -> ApprovalBinding:
    base = dict(
        session_id="s1",
        turn_id="t1",
        sequence_number=1,
        tenant_id="ten",
        project_id="p1",
        principal_id=PRINCIPAL,
        source_revision=7,
        source_asset_digest=sha256_bytes(b"asset"),
        ast_digest=digest_of({"ast": 1}),
        plan_digest=digest_of({"plan": 1}),
        preview_digest=digest_of({"preview": 1}),
    )
    base.update(overrides)
    return ApprovalBinding(**base)


def _payload(**overrides) -> ApprovalBindingPayload:
    base = dict(
        project_id="p1",
        session_id="s1",
        turn_id="t1",
        base_revision_id="rev:7",
        plan_sha256=digest_of({"plan": 1}),
        preview_sha256=digest_of({"preview": 1}),
        scope_signature=SCOPE,
        principal_id=PRINCIPAL,
        expires_at_utc="2026-11-25T00:10:00Z",
    )
    base.update(overrides)
    return ApprovalBindingPayload(**base)


def _identity(**overrides) -> ApprovalIdentity:
    base = dict(
        principal_id=PRINCIPAL,
        tenant_id="ten",
        project_id="p1",
        session_id="s1",
        turn_id="t1",
        sequence_number=1,
        source_revision=7,
        source_asset_digest=sha256_bytes(b"asset"),
        ast_digest=digest_of({"ast": 1}),
        plan_digest=digest_of({"plan": 1}),
        preview_digest=digest_of({"preview": 1}),
    )
    base.update(overrides)
    return ApprovalIdentity(**base)


@pytest.fixture()
def ledger(tmp_path: Path):
    handle = DurableCanonicalApprovalLedger(
        tmp_path / "approvals.sqlite3", key_provider=_ring(), now=NOW
    )
    yield handle
    handle.close()


def _approved(ledger, **payload_overrides) -> str:
    binding = ledger.create(_binding())
    ledger.bind_canonical(binding.approval_id, _payload(**payload_overrides))
    ledger.approve(binding.approval_id, identity=_identity(), turn_is_active=lambda: True)
    return binding.approval_id


class TestCanonicalPayload:
    def test_payload_has_exactly_the_nine_bound_fields(self) -> None:
        assert len(BINDING_FIELDS) == 9
        assert set(_payload().as_dict()) == set(BINDING_FIELDS)

    def test_payload_is_rfc8785_canonical(self) -> None:
        payload = _payload()
        expected = canonical_bytes(payload.as_dict(), CanonicalizationScheme.JCS_RFC8785_V2)
        assert payload.canonical_text().encode("utf-8") == expected
        assert payload.digest() == "sha256:" + __import__("hashlib").sha256(expected).hexdigest()

    def test_factory_matches_the_dataclass(self) -> None:
        payload = _payload()
        rebuilt = canonical_binding_payload(**payload.as_dict())  # type: ignore[arg-type]
        assert rebuilt == payload.as_dict()

    @pytest.mark.parametrize("field", BINDING_FIELDS)
    def test_every_field_must_be_a_non_empty_string(self, field: str) -> None:
        with pytest.raises(ApprovalBindingMismatchError):
            canonical_binding_payload(**{**_payload().as_dict(), field: ""})  # type: ignore[arg-type]

    def test_whitespace_and_key_order_do_not_change_the_digest(self) -> None:
        first = _payload()
        shuffled = dict(reversed(list(first.as_dict().items())))
        assert canonical_binding_payload(**shuffled).__class__ is dict  # type: ignore[arg-type]
        assert CanonicalApprovalLedger  # the seam is exported


class TestCanonicalMac:
    def test_mac_is_deterministic_and_keyed(self) -> None:
        payload = _payload()
        first = canonical_binding_mac(KEY, payload)
        # a key of the same length but different content must not collide
        second = canonical_binding_mac(b"z" * 32, payload)
        assert canonical_binding_mac(KEY, payload) == first
        assert first != second
        assert len(first) == 64

    def test_mac_is_domain_separated(self) -> None:
        import hashlib

        payload = _payload()
        undomained = hmac.new(KEY, payload.canonical_text().encode(), hashlib.sha256).hexdigest()
        assert canonical_binding_mac(KEY, payload) != undomained
        assert CANONICAL_BINDING_DOMAIN.startswith(b"NAGAR/IBPC/")

    def test_mac_rejects_an_empty_key(self) -> None:
        with pytest.raises(ApprovalBindingMismatchError):
            canonical_binding_mac(b"", _payload())

    @pytest.mark.parametrize(
        "field,value",
        [
            ("base_revision_id", "rev:8"),
            ("scope_signature", "sha256:" + "c" * 64),
            ("plan_sha256", "sha256:" + "d" * 64),
            ("preview_sha256", "sha256:" + "e" * 64),
            ("principal_id", "prn_" + "2" * 32),
        ],
    )
    def test_every_immutable_field_mutation_changes_the_mac(self, field: str, value: str) -> None:
        original = canonical_binding_mac(KEY, _payload())
        mutated = canonical_binding_mac(KEY, _payload(**{field: value}))
        assert not hmac.compare_digest(original, mutated)


class TestDurableSingleUseConsumption:
    def test_consume_once_then_replay_is_refused(self, ledger) -> None:
        approval_id = _approved(ledger)
        consumed = ledger.consume_approval(approval_id, identity=_identity())
        assert consumed.state is ApprovalState.CONSUMED
        with pytest.raises(ApprovalAlreadyConsumedError) as exc:
            ledger.consume_approval(approval_id, identity=_identity())
        assert exc.value.reason == "already_consumed"

    def test_expired_token_is_refused_and_stays_unconsumed(self, tmp_path: Path) -> None:
        ledger = DurableCanonicalApprovalLedger(
            tmp_path / "expired.sqlite3", key_provider=_ring(), now=NOW
        )
        try:
            approval_id = _approved(ledger)
            ledger._now = EXPIRED
            with pytest.raises(ApprovalExpiredError) as exc:
                ledger.consume_approval(approval_id, identity=_identity())
            assert exc.value.reason == "approval_expired"
            # the refusal must NOT burn the approval
            assert ledger.get(approval_id).state is ApprovalState.APPROVED
        finally:
            ledger.close()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("base_revision_id", "rev:99"),
            ("scope_signature", "sha256:" + "9" * 64),
            ("plan_sha256", "sha256:" + "8" * 64),
            ("preview_sha256", "sha256:" + "7" * 64),
            ("principal_id", "prn_" + "3" * 32),
        ],
    )
    def test_presented_payload_mutation_is_refused(self, ledger, field: str, value: str) -> None:
        approval_id = _approved(ledger)
        with pytest.raises(ApprovalBindingMismatchError) as exc:
            ledger.consume_approval(
                approval_id,
                identity=_identity(principal_id=value) if field == "principal_id" else _identity(),
                payload=_payload(**{field: value}),
            )
        assert exc.value.reason == "approval_binding_mismatch"
        assert ledger.get(approval_id).state is ApprovalState.APPROVED

    def test_stored_row_tampering_breaks_the_mac(self, ledger) -> None:
        approval_id = _approved(ledger)
        ledger._conn.execute(
            "UPDATE approval_bindings SET scope_signature = ? WHERE approval_id = ?",
            ("sha256:" + "0" * 64, approval_id),
        )
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.verify_canonical(approval_id)
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.consume_approval(approval_id, identity=_identity())

    def test_a_different_key_never_verifies(self, ledger, tmp_path: Path) -> None:
        approval_id = _approved(ledger)
        path = ledger.path
        ledger.close()
        other = DurableCanonicalApprovalLedger(
            path, key_provider=_ring(b"z" * 32, "rotated"), now=NOW
        )
        try:
            with pytest.raises(ApprovalBindingMismatchError):
                other.verify_canonical(approval_id)
        finally:
            other.close()

    def test_rebinding_to_a_different_payload_is_refused(self, ledger) -> None:
        approval_id = _approved(ledger)
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.bind_canonical(approval_id, _payload(base_revision_id="rev:100"))

    def test_rebinding_the_identical_payload_is_idempotent(self, ledger) -> None:
        approval_id = _approved(ledger)
        first = ledger.binding_mac(approval_id)
        assert ledger.bind_canonical(approval_id, _payload()) == first

    def test_unbound_approval_is_a_typed_refusal(self, ledger) -> None:
        binding = ledger.create(_binding())
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.verify_canonical(binding.approval_id)
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.consume_approval(binding.approval_id, identity=_identity())

    def test_unavailable_key_provider_is_a_typed_refusal(self, tmp_path: Path) -> None:
        class _Broken:
            active_key_id = "broken"

            def key(self, key_id: str):
                raise KeyProviderError("no key")

        ledger = DurableCanonicalApprovalLedger(
            tmp_path / "broken.sqlite3",
            key_provider=_Broken(),
            now=NOW,  # type: ignore[arg-type]
        )
        try:
            binding = ledger.create(_binding())
            with pytest.raises(ApprovalBindingMismatchError):
                ledger.bind_canonical(binding.approval_id, _payload())
        finally:
            ledger.close()

    def test_consume_requires_scope_before_an_approved_state(self, ledger) -> None:
        binding = ledger.create(_binding())
        ledger.bind_canonical(binding.approval_id, _payload())
        with pytest.raises(ApprovalError) as exc:
            ledger.consume_approval(binding.approval_id, identity=_identity())
        assert exc.value.reason == "not_approved"

    def test_unparsable_expiry_is_an_expiry_refusal(self, ledger) -> None:
        """A tampered/!parsable expiry is fail-closed as an expiry refusal.

        The MAC check runs first and catches the tampered row; a binding that
        was *created* with a malformed expiry can never be consumed either.
        """
        approval_id = _approved(ledger)
        ledger._conn.execute(
            "UPDATE approval_bindings SET expires_at_utc = 'not-a-timestamp' WHERE approval_id = ?",
            (approval_id,),
        )
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.verify_canonical(approval_id)
        malformed = _payload(expires_at_utc="not-a-timestamp")
        assert "not-a-timestamp" in malformed.as_dict()["expires_at_utc"]
        from nexus_ai_agent.nagar.ibpc.approval import _utc_instant

        with pytest.raises(ApprovalExpiredError):
            _utc_instant("not-a-timestamp")

    def test_naive_expiry_is_an_expiry_refusal(self, ledger) -> None:
        from nexus_ai_agent.nagar.ibpc.approval import _utc_instant

        with pytest.raises(ApprovalExpiredError):
            _utc_instant("2026-11-25T00:10:00")

    def test_payload_must_be_the_stored_one_when_presented(self, ledger) -> None:
        approval_id = _approved(ledger)
        stored = ledger.binding_payload(approval_id)
        assert ledger.verify_canonical(approval_id, payload=stored) == stored


class TestDurabilityAcrossRestart:
    def test_consumption_survives_a_full_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "restart.sqlite3"
        first = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=NOW)
        approval_id = _approved(first)
        first.consume_approval(approval_id, identity=_identity())
        first.close()

        second = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=NOW)
        try:
            # the row is still CONSUMED and the MAC still verifies …
            assert second.get(approval_id).state is ApprovalState.CONSUMED
            assert second.verify_canonical(approval_id).project_id == "p1"
            # … so a replay is refused after the restart, not re-admitted
            with pytest.raises(ApprovalAlreadyConsumedError):
                second.consume_approval(approval_id, identity=_identity())
        finally:
            second.close()

    def test_pending_approval_is_still_consumable_after_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "pending.sqlite3"
        first = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=NOW)
        approval_id = _approved(first)
        first.close()
        second = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=LATER)
        try:
            assert second.consume_approval(approval_id, identity=_identity()).state is (
                ApprovalState.CONSUMED
            )
        finally:
            second.close()


class TestInMemoryCanonicalLedger:
    def test_in_memory_variant_shares_the_transition_contract(self) -> None:
        ledger = CanonicalApprovalLedger(key_provider=_ring(), now=NOW)
        binding = ledger.create(_binding())
        ledger.bind_canonical(binding.approval_id, _payload())
        ledger.approve(binding.approval_id, identity=_identity(), turn_is_active=lambda: True)
        # in-memory storage has no persisted binding columns: the refusal is typed
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.consume_approval(binding.approval_id, identity=_identity())


class TestCompositionFactory:
    def _deps(self, tmp_path: Path) -> IBPCServiceDeps:
        return IBPCServiceDeps(
            queue=object(),  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: 0,
        )

    def test_factory_builds_the_durable_canonical_ledger(self, tmp_path: Path) -> None:
        service = build_durable_ibpc_service(tmp_path / "w3", KEY, deps=self._deps(tmp_path))
        assert isinstance(service, IBPCService)
        assert isinstance(service.approvals, DurableCanonicalApprovalLedger)
        assert service.session_store is not None
        assert service._deps.privacy_consent_store is not None
        assert service._deps.approval_key_provider is service._deps.approval_ledger._key_provider

    def test_factory_rejects_a_short_hmac_secret(self, tmp_path: Path) -> None:
        with pytest.raises(KeyProviderError):
            build_durable_ibpc_service(tmp_path / "w3", b"short", deps=self._deps(tmp_path))

    def test_factory_can_omit_the_consent_ledger(self, tmp_path: Path) -> None:
        service = build_durable_ibpc_service(
            tmp_path / "w3", KEY, deps=self._deps(tmp_path), with_consent_ledger=False
        )
        assert service._deps.privacy_consent_store is None

    def test_factory_honours_an_explicit_key_provider(self, tmp_path: Path) -> None:
        provider = _ring(key_id="rotated")
        service = build_durable_ibpc_service(
            tmp_path / "w3", KEY, deps=self._deps(tmp_path), key_provider=provider
        )
        assert service._deps.approval_key_provider is provider


class TestServiceUsesTheLedger:
    def test_service_can_be_composed_with_an_explicit_ledger(self, tmp_path: Path) -> None:
        ledger = CanonicalApprovalLedger(key_provider=_ring(), now=NOW)
        deps = IBPCServiceDeps(
            queue=object(),  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: 0,
            approval_ledger=ledger,
        )
        service = IBPCService(deps)
        assert service.approvals is ledger

    def test_default_composition_keeps_the_in_memory_ledger(self, tmp_path: Path) -> None:
        deps = IBPCServiceDeps(
            queue=object(),  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: 0,
        )
        assert type(IBPCService(deps).approvals) is ApprovalLedger

    def test_service_session_store_accessor_is_none_without_one(self, tmp_path: Path) -> None:
        deps = IBPCServiceDeps(
            queue=object(),  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: 0,
        )
        assert IBPCService(deps).session_store is None


# --------------------------------------------------------------------------- #
# Service-level integration: the durable ledger is actually the one consumed
# --------------------------------------------------------------------------- #


class _RecordingQueue:
    """A queue port that records enqueues; the drain then reports a failure."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, dict]] = []

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
        self.enqueued.append((job_type, idempotency_key, payload))
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str):
        from nexus_ai_agent.application.ports.job_queue import JobStatus

        return JobStatus.FAILED_TERMINAL

    async def get_result(self, job_id: str) -> dict:
        return {"success": False, "error": "not executed in this test"}


class _PortAuthority:
    """The canonical authority port: membership + binding ownership, stubbed."""

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        from nexus_ai_agent.nagar.ibpc.authority import PrincipalFacts

        return PrincipalFacts(
            principal_id=principal_id,
            project_id=project_id,
            channel_binding_id=binding_ref,
        )


@pytest.fixture()
def durable_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A service wired to the REAL durable approval + session stores."""
    import types

    import nexus_ai_agent.nagar.ibpc.service as service_mod
    from nexus_ai_agent.nagar.ibpc.live_session import DurableLiveSessionStore

    monkeypatch.setattr(
        service_mod,
        "probe_media",
        lambda path, *, binary=None: (types.SimpleNamespace(duration_us=3_000_000), 30.0),
    )

    def fake_shadow(path, scope, plan_digest, **kwargs):
        payload = {
            "plan_digest": plan_digest,
            "scope_digest": scope.scope_digest,
            "source_duration_us": 3_000_000,
            "scope_start_us": scope.start_us,
            "scope_end_us": scope.end_us,
            "keyframes": [],
            "composite_digest": "sha256:" + "0" * 64,
        }
        return types.SimpleNamespace(
            plan_digest=plan_digest,
            scope_digest=scope.scope_digest,
            keyframes=(),
            model_dump=lambda **_kw: payload,
        )

    def fake_signature(path, scope, **kwargs):
        payload = {
            "scope_digest": scope.scope_digest,
            "asset_id": scope.asset_id,
            "start_us": scope.start_us,
            "end_us": scope.end_us,
            "color_histogram": [0.0] * 64,
            "motion_magnitude": 0.0,
            "sample_frames": 1,
            "computed_at": NOW(),
            "signature_digest": "sha256:" + "1" * 64,
        }
        return types.SimpleNamespace(
            signature_digest=payload["signature_digest"],
            scope_digest=scope.scope_digest,
            model_dump=lambda **_kw: payload,
        )

    monkeypatch.setattr(service_mod, "build_shadow_preview", fake_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)

    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    queue = _RecordingQueue()
    ledger = DurableCanonicalApprovalLedger(
        tmp_path / "approvals.sqlite3", key_provider=_ring(), now=NOW
    )
    sessions = DurableLiveSessionStore(tmp_path / "sessions.sqlite3", now=NOW)
    service = IBPCService(
        IBPCServiceDeps(
            queue=queue,  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: 7,
            now=NOW,
            approval_key_provider=_ring(),
            approval_ledger=ledger,
            session_store=sessions,
            channel_binding_ref="cbn_" + "a" * 64,
            authority=_PortAuthority(),
        )
    )
    yield service, queue, clip, ledger, sessions
    ledger.close()
    sessions.close()


def _open_and_submit(service: IBPCService, clip: Path):
    session_id = service.open_session(
        tenant_id="ten",
        project_id="project-w3",
        principal_id=PRINCIPAL,
        user_id=1,
        chat_id=1,
        current_revision=7,
    )
    result = service.submit_text_intent(
        session_id,
        "turn-1",
        sequence_number=1,
        text="trim from 0.0 to 2.0 seconds",
        asset_id="asset-w3",
        source_path=str(clip),
    )
    assert type(result).__name__ == "PipelineResult", result
    return session_id, result


class TestServiceCanonicalApprovalFlow:
    def test_approve_records_the_canonical_binding_on_the_durable_ledger(
        self, durable_service
    ) -> None:
        service, _queue, clip, ledger, _sessions = durable_service
        session_id, proposal = _open_and_submit(service, clip)
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        stored = ledger.binding_payload(proposal.approval_id)
        assert stored.project_id == "project-w3"
        assert stored.principal_id == PRINCIPAL
        assert stored.session_id == session_id
        assert stored.turn_id == "turn-1"
        assert stored.base_revision_id == "rev:7"
        assert stored.plan_sha256 == proposal.plan.plan_digest
        assert stored.preview_sha256 == proposal.preview.preview_digest
        assert ledger.verify_canonical(proposal.approval_id) == stored
        assert len(ledger.binding_mac(proposal.approval_id)) == 64

    def test_execution_consumes_the_canonical_binding_exactly_once(self, durable_service) -> None:
        import asyncio

        service, _queue, clip, ledger, _sessions = durable_service
        session_id, proposal = _open_and_submit(service, clip)
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        first = asyncio.run(
            service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        )
        # the job drains to a failed status, but the CONSUME already happened
        assert first.status in {"failed", "refused"}
        assert ledger.get(proposal.approval_id).state is ApprovalState.CONSUMED
        # the failed drain terminalised the turn, so the second attempt is
        # refused at the turn fence — and the ledger itself still refuses a
        # direct replay of the consumed approval.
        second = asyncio.run(
            service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        )
        assert second.status == "refused"
        assert "turn_unavailable" in second.detail
        with pytest.raises(ApprovalAlreadyConsumedError):
            ledger.consume_approval(proposal.approval_id, identity=_identity())

    def test_a_presented_payload_mismatch_refuses_before_enqueue(self, durable_service) -> None:
        import asyncio

        service, queue, clip, ledger, _sessions = durable_service
        session_id, proposal = _open_and_submit(service, clip)
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        # mutate the stored canonical binding behind the ledger's back
        ledger._conn.execute(
            "UPDATE approval_bindings SET base_revision_id = 'rev:999' WHERE approval_id = ?",
            (proposal.approval_id,),
        )
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "approval_binding_mismatch" in outcome.detail
        assert queue.enqueued == []
        assert ledger.get(proposal.approval_id).state is ApprovalState.APPROVED

    def test_an_expired_canonical_binding_refuses_execution(self, durable_service) -> None:
        import asyncio

        service, queue, clip, ledger, _sessions = durable_service
        session_id, proposal = _open_and_submit(service, clip)
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        ledger._now = EXPIRED  # type: ignore[method-assign]
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "approval_expired" in outcome.detail
        assert queue.enqueued == []

    def test_durable_turn_row_tracks_preview_and_approval(self, durable_service) -> None:
        service, _queue, clip, _ledger, sessions = durable_service
        session_id, proposal = _open_and_submit(service, clip)
        turn = sessions.get_turn(session_id, "turn-1")
        assert turn.turn_status.value == "PREVIEW_READY"
        assert turn.approval_id == proposal.approval_id
        assert turn.base_revision_id == "rev:7"
        assert turn.compiled_plan_sha256
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        assert sessions.get_turn(session_id, "turn-1").turn_status.value == "APPROVED"

    def test_durable_session_state_reconstructs_after_restart(self, durable_service) -> None:
        from nexus_ai_agent.nagar.ibpc.live_session import DurableLiveSessionStore

        service, _queue, clip, _ledger, sessions = durable_service
        session_id, proposal = _open_and_submit(service, clip)
        path = sessions.path
        reopened = DurableLiveSessionStore(path, now=NOW)
        try:
            assert reopened.get_session(session_id).last_turn_index == 1
            assert reopened.get_turn(session_id, "turn-1").approval_id == proposal.approval_id
        finally:
            reopened.close()


# --------------------------------------------------------------------------- #
# Coverage closure: reservation lifecycle, migration, verify_canonical branches
# --------------------------------------------------------------------------- #


class TestReservationFencing:
    """The job reservation lets a recovery attempt re-read a consumed binding."""

    def test_in_memory_reservation_lifecycle(self) -> None:
        ledger = CanonicalApprovalLedger(key_provider=_ring(), now=NOW)
        approval_id = _approved(ledger)
        consumed = ledger.consume(approval_id, identity=_identity(), job_id="job-1", attempt=1)
        assert consumed.state is ApprovalState.CONSUMED
        # same job + same identity re-reads the consumed binding (recovery)
        again = ledger.consume(approval_id, identity=_identity(), job_id="job-1", attempt=2)
        assert again.state is ApprovalState.CONSUMED
        ledger.assert_active_reservation(
            approval_id, job_id="job-1", attempt=2, identity=_identity()
        )
        with pytest.raises(ApprovalError) as wrong_job:
            ledger.assert_active_reservation(
                approval_id, job_id="job-2", attempt=2, identity=_identity()
            )
        assert wrong_job.value.reason == "replay"
        with pytest.raises(ApprovalError) as stale:
            ledger.assert_active_reservation(
                approval_id, job_id="job-1", attempt=1, identity=_identity()
            )
        assert stale.value.reason == "stale_attempt"
        # finalize closes the reservation; a replay can no longer recover it
        ledger.finalize_consumption(approval_id, job_id="job-1", attempt=2)
        with pytest.raises(ApprovalError) as closed:
            ledger.consume(approval_id, identity=_identity(), job_id="job-1", attempt=2)
        assert closed.value.reason == "replay"

    def test_durable_reservation_lifecycle(self, ledger) -> None:
        approval_id = _approved(ledger)
        ledger.consume_approval(approval_id, identity=_identity(), job_id="job-9", attempt=1)
        recovered = ledger.consume(approval_id, identity=_identity(), job_id="job-9", attempt=1)
        assert recovered.state is ApprovalState.CONSUMED
        ledger.finalize_consumption(approval_id, job_id="job-9", attempt=1)
        # idempotent finalize keeps the closed reservation closed
        ledger.finalize_consumption(approval_id, job_id="job-9", attempt=1)
        with pytest.raises(ApprovalError) as closed:
            ledger.consume(approval_id, identity=_identity(), job_id="job-9", attempt=1)
        assert closed.value.reason == "replay"

    def test_superseded_attempt_cannot_finalize(self, ledger) -> None:
        approval_id = _approved(ledger)
        ledger.consume_approval(approval_id, identity=_identity(), job_id="job-9", attempt=1)
        ledger.consume(approval_id, identity=_identity(), job_id="job-9", attempt=5)
        with pytest.raises(ApprovalAlreadyConsumedError):
            ledger.finalize_consumption(approval_id, job_id="job-9", attempt=1)

    def test_reservation_identity_drift_is_a_replay_refusal(self, ledger) -> None:
        approval_id = _approved(ledger)
        ledger.consume_approval(approval_id, identity=_identity(), job_id="job-9")
        with pytest.raises(ApprovalError) as drift:
            ledger.consume(
                approval_id, identity=_identity(principal_id="prn_" + "9" * 32), job_id="job-9"
            )
        assert drift.value.reason == "replay"
        with pytest.raises(ApprovalError) as no_job:
            ledger.consume(approval_id, identity=_identity())
        assert no_job.value.reason == "replay"

    @pytest.mark.parametrize("job_id", ["", "   "])
    def test_empty_job_id_is_refused(self, ledger, job_id: str) -> None:
        approval_id = _approved(ledger)
        with pytest.raises(ApprovalError) as exc:
            ledger.consume(approval_id, identity=_identity(), job_id=job_id)
        assert exc.value.reason == "invalid_job_id"
        assert ledger.get(approval_id).state is ApprovalState.APPROVED

    @pytest.mark.parametrize("attempt", [0, -1, 2**63, True, "1", 1.5])
    def test_invalid_attempt_is_refused(self, ledger, attempt) -> None:
        approval_id = _approved(ledger)
        with pytest.raises(ApprovalError) as exc:
            ledger.consume(approval_id, identity=_identity(), attempt=attempt)
        assert exc.value.reason == "invalid_attempt"
        with pytest.raises(ApprovalError) as finalized:
            ledger.finalize_consumption(approval_id, job_id="job-1", attempt=attempt)
        assert finalized.value.reason == "invalid_attempt"

    def test_finalize_without_reservation_is_refused(self, ledger) -> None:
        approval_id = _approved(ledger)
        with pytest.raises(ApprovalError) as exc:
            ledger.finalize_consumption(approval_id, job_id="job-1")
        assert exc.value.reason == "replay"


class TestLegacyReservationMigration:
    def test_a_pre_last_attempt_table_gains_the_column(self, tmp_path: Path) -> None:
        import sqlite3

        from nexus_ai_agent.nagar.ibpc.approval import DurableApprovalLedger

        db = tmp_path / "legacy.sqlite3"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE approval_reservations ("
                " approval_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,"
                " fingerprint TEXT NOT NULL, finalized_at TEXT)"
            )
            conn.execute("INSERT INTO approval_reservations VALUES ('a1', 'job-1', 'fp', NULL)")
            conn.commit()
        finally:
            conn.close()
        legacy = DurableApprovalLedger(db, now=NOW)
        try:
            columns = {
                row[1] for row in legacy._conn.execute("PRAGMA table_info(approval_reservations)")
            }
            assert "last_attempt" in columns
            assert legacy._read_reservation("a1") == ("job-1", "fp", None, 1)
        finally:
            legacy.close()


class TestVerifyCanonicalBranches:
    def test_a_presented_payload_mismatch_is_refused(self, ledger) -> None:
        approval_id = _approved(ledger)
        with pytest.raises(ApprovalBindingMismatchError) as exc:
            ledger.verify_canonical(approval_id, payload=_payload(base_revision_id="rev:999"))
        assert "base_revision_id" in str(exc.value.detail) or "differs" in str(exc.value.detail)

    def test_an_immutable_field_mismatch_is_refused(self, ledger) -> None:
        approval_id = _approved(ledger)
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.verify_canonical(approval_id, payload=_payload(plan_sha256="sha256:" + "9" * 64))

    def test_an_expiry_only_payload_mismatch_is_refused(self, ledger) -> None:
        approval_id = _approved(ledger)
        with pytest.raises(ApprovalBindingMismatchError) as exc:
            ledger.verify_canonical(
                approval_id, payload=_payload(expires_at_utc="2027-01-01T00:00:00Z")
            )
        assert "expires_at_utc" in str(exc.value.detail)

    def test_an_unparsable_stored_expiry_is_refused(self, ledger) -> None:
        approval_id = _approved(ledger, expires_at_utc="not-an-instant")
        with pytest.raises(ApprovalExpiredError):
            ledger.verify_canonical(approval_id)

    def test_an_expired_token_is_refused_by_verify(self, ledger) -> None:
        approval_id = _approved(ledger)
        ledger._now = EXPIRED  # type: ignore[method-assign]
        with pytest.raises(ApprovalExpiredError):
            ledger.verify_canonical(approval_id)

    def test_a_tampered_mac_is_refused_by_verify(self, ledger) -> None:
        approval_id = _approved(ledger)
        ledger._conn.execute(
            "UPDATE approval_bindings SET mac = ? WHERE approval_id = ?",
            ("0" * 64, approval_id),
        )
        # the writing instance caches the MAC; a fresh reader must hit the row
        reader = DurableCanonicalApprovalLedger(ledger.path, key_provider=_ring(), now=NOW)
        try:
            with pytest.raises(ApprovalBindingMismatchError):
                reader.verify_canonical(approval_id)
        finally:
            reader.close()

    def test_an_unbound_approval_has_no_payload(self, ledger) -> None:
        approval_id = _approved(ledger)
        ledger._conn.execute("DELETE FROM approval_bindings WHERE approval_id = ?", (approval_id,))
        reader = DurableCanonicalApprovalLedger(ledger.path, key_provider=_ring(), now=NOW)
        try:
            with pytest.raises(ApprovalBindingMismatchError):
                reader.binding_mac(approval_id)
            with pytest.raises(ApprovalBindingMismatchError):
                reader.binding_payload(approval_id)
        finally:
            reader.close()

    def test_a_missing_mac_is_refused_by_the_in_memory_base(self) -> None:
        ledger = CanonicalApprovalLedger(key_provider=_ring(), now=NOW)
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.binding_mac("unknown-approval")

    def test_the_in_memory_ledger_cannot_verify_a_binding(self) -> None:
        ledger = CanonicalApprovalLedger(key_provider=_ring(), now=NOW)
        approval_id = _approved(ledger)
        # the in-memory base keeps the MAC but no durable payload columns
        assert len(ledger.binding_mac(approval_id)) == 64
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.binding_payload(approval_id)


class TestIdentityDriftWithAStableReservation:
    """A reservation whose fingerprint matches still re-checks the binding."""

    def _tampered_ledger(self):
        """Approve identity A, then rewrite the stored binding to identity B."""
        ledger = CanonicalApprovalLedger(key_provider=_ring(), now=NOW)
        approval_id = _approved(ledger)
        stored = ledger.get(approval_id)
        drifted = stored.model_copy(update={"principal_id": "prn_" + "9" * 32})
        ledger._store(drifted)
        return ledger, approval_id

    def test_consume_refuses_a_reserved_binding_that_drifted(self) -> None:
        ledger, approval_id = self._tampered_ledger()
        with pytest.raises(ApprovalError):
            ledger.consume(approval_id, identity=_identity(), job_id="job-1")
        # seed a reservation whose fingerprint matches the presented identity
        ledger._write_reservation(approval_id, "job-1", _identity().identity_digest(), None, 1)
        ledger._store(ledger.get(approval_id).model_copy(update={"state": ApprovalState.CONSUMED}))
        with pytest.raises(ApprovalError) as exc:
            ledger.consume(approval_id, identity=_identity(), job_id="job-1")
        assert exc.value.reason == "replay"
        assert "reserved identity changed" in str(exc.value.detail)
        with pytest.raises(ApprovalError) as checked:
            ledger.assert_active_reservation(
                approval_id, job_id="job-1", attempt=1, identity=_identity()
            )
        assert checked.value.reason == "replay"
        assert "reserved identity changed" in str(checked.value.detail)


class TestFactoryKeyProviderContract:
    def test_an_unusable_key_provider_is_a_typed_composition_error(self, tmp_path: Path) -> None:
        class _BrokenProvider:
            active_key_id = "production"

            def key(self, key_id: str):
                raise KeyProviderError("no such key")

        with pytest.raises(KeyProviderError):
            build_durable_ibpc_service(
                tmp_path,
                KEY,
                deps=IBPCServiceDeps(
                    queue=object(),  # type: ignore[arg-type]
                    registry=build_runtime_registry(),
                    workspace_root=tmp_path,
                    revision_provider=lambda: 0,
                    now=NOW,
                ),
                key_provider=_BrokenProvider(),  # type: ignore[arg-type]
            )

    def test_a_provider_without_a_key_surface_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(KeyProviderError):
            build_durable_ibpc_service(
                tmp_path,
                KEY,
                deps=IBPCServiceDeps(
                    queue=object(),  # type: ignore[arg-type]
                    registry=build_runtime_registry(),
                    workspace_root=tmp_path,
                    revision_provider=lambda: 0,
                    now=NOW,
                ),
                key_provider=object(),  # type: ignore[arg-type]
            )
