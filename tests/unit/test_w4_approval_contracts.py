"""Pure approval claims and durable port contracts (not an authority)."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore
from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding, DurablePlanDedupePort


def make_binding(**changes: Any) -> ApprovalBinding:
    fields: dict[str, Any] = {
        "project_id": "project",
        "plan_id": "plan",
        "plan_hash": "a" * 64,
        "actor_id": "actor",
        "approval_id": "approval",
        "step_id": "step",
        "capability_version": "v1",
        "normalized_params_hash": "b" * 64,
        "policy_version": "policy-v1",
        "nonce": "nonce",
        "issued_at": 10.0,
        "expires_at": 20.0,
    }
    fields.update(changes)
    return ApprovalBinding(**fields)


def test_claims_are_immutable_but_not_self_authenticating(tmp_path: Path) -> None:
    binding = make_binding()
    with pytest.raises(FrozenInstanceError):
        binding.plan_id = "other"  # type: ignore[misc]
    store: DurablePlanDedupePort = DurablePlanDedupeStore(tmp_path / "db")
    assert (
        store.reserve(
            binding.plan_id,
            binding.plan_hash,
            external_effects=False,
            project_id=binding.project_id,
        ).status
        == "RESERVED"
    )


@pytest.mark.parametrize(
    "field",
    [
        "project_id",
        "plan_id",
        "actor_id",
        "approval_id",
        "step_id",
        "capability_version",
        "policy_version",
        "nonce",
    ],
)
@pytest.mark.parametrize("value", ["", " ", "a" * 257, None])
def test_bad_claim_identities_refused(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        make_binding(**{field: value})


@pytest.mark.parametrize("field", ["plan_hash", "normalized_params_hash"])
@pytest.mark.parametrize("value", [None, "", "A" * 64, "a" * 63, "g" * 64])
def test_bad_digest_refused(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match=field):
        make_binding(**{field: value})


@pytest.mark.parametrize("field", ["issued_at", "expires_at"])
@pytest.mark.parametrize("value", [True, "10", float("nan"), float("inf"), 10**400])
def test_bad_time_refused(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        make_binding(**{field: value})


@pytest.mark.parametrize("expiry", [10, 9])
def test_non_increasing_expiry_refused(expiry: float) -> None:
    with pytest.raises(ValueError, match="expiry"):
        make_binding(expires_at=expiry)


class Verifier:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    def verify(self, binding: ApprovalBinding) -> bool:
        self.calls += 1
        return self.result  # type: ignore[no-any-return]


def test_approval_binding_requires_trusted_verifier_and_current_matching_claims() -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import (
        ApprovalBindingError,
        verify_approval_binding,
    )

    binding = make_binding()
    verifier = Verifier(True)
    args = {
        "project_id": "project",
        "plan_id": "plan",
        "plan_hash": "a" * 64,
        "actor_id": "actor",
        "step_id": "step",
        "capability_version": "v1",
        "normalized_params_hash": "b" * 64,
        "policy_version": "policy-v1",
        "now": 10.0,
        "verifier": verifier,
    }
    verify_approval_binding(binding, **args)  # type: ignore[arg-type]
    assert verifier.calls == 1
    for field, value in (
        ("project_id", "other"),
        ("plan_id", "other"),
        ("plan_hash", "b" * 64),
        ("actor_id", "other"),
        ("step_id", "other"),
        ("capability_version", "v2"),
        ("normalized_params_hash", "c" * 64),
        ("policy_version", "policy-v2"),
        ("now", 9),
        ("now", 20),
        ("now", True),
        ("now", float("nan")),
        ("now", "10"),
    ):
        with pytest.raises(ApprovalBindingError):
            verify_approval_binding(binding, **{**args, field: value})  # type: ignore[arg-type]
    assert verifier.calls == 1
    with pytest.raises(ApprovalBindingError, match="invalid"):
        verify_approval_binding(None, **args)  # type: ignore[arg-type]
    for result in (False, 1, None):
        with pytest.raises(ApprovalBindingError, match="authority"):
            verify_approval_binding(binding, **{**args, "verifier": Verifier(result)})  # type: ignore[arg-type]


def test_effect_key_is_versioned_canonical_and_scope_bound() -> None:
    from nexus_ai_agent.creative.studio.plan_identifiers import derive_effect_key

    args = {
        "project_id": "پروژه",
        "plan_id": "plan",
        "node_id": "node",
        "idempotency_key": "idempotency",
        "fingerprint": "a" * 64,
    }
    key = derive_effect_key(**args)
    assert key == "b272453d484e2ae7a386719ad2131a350e812472fe1d800279e4b35cc847e434"
    for name in ("project_id", "plan_id", "node_id", "idempotency_key"):
        assert derive_effect_key(**{**args, name: "other"}) != key
    assert derive_effect_key(**{**args, "fingerprint": "b" * 64}) != key
    # Concatenation boundaries cannot collide.
    assert derive_effect_key(**{**args, "plan_id": "ab", "node_id": "c"}) != derive_effect_key(
        **{**args, "plan_id": "a", "node_id": "bc"}
    )


@pytest.mark.parametrize("name", ["project_id", "plan_id", "node_id", "idempotency_key"])
@pytest.mark.parametrize("invalid", ["", "  ", "é" * 129, "\ud800", None])
def test_effect_key_refuses_invalid_identities(name: str, invalid: object) -> None:
    from nexus_ai_agent.creative.studio.plan_identifiers import derive_effect_key

    args = {
        "project_id": "project",
        "plan_id": "plan",
        "node_id": "node",
        "idempotency_key": "key",
        "fingerprint": "a" * 64,
    }
    with pytest.raises(ValueError):
        derive_effect_key(**{**args, name: invalid})  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [None, "A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n"])
def test_effect_key_refuses_invalid_fingerprints(invalid: object) -> None:
    from nexus_ai_agent.creative.studio.plan_identifiers import derive_effect_key

    with pytest.raises(ValueError, match="fingerprint"):
        derive_effect_key(
            project_id="project",
            plan_id="plan",
            node_id="node",
            idempotency_key="key",
            fingerprint=invalid,  # type: ignore[arg-type]
        )
