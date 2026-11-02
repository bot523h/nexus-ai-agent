"""Unit tests: privacy modes, consent records, log redaction (M4)."""

from __future__ import annotations

import pytest

from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes
from nexus_ai_agent.nagar.ibpc.models import PrivacyMode
from nexus_ai_agent.nagar.ibpc.privacy import (
    PrivacyPolicy,
    PrivacyViolation,
    redact_mapping,
)

NOW = lambda: "2026-10-31T00:00:00Z"  # noqa: E731


class TestRedaction:
    def test_sensitive_fields_become_digests_only(self) -> None:
        record = {
            "transcript": "secret words",
            "audio": b"\x00\x01",
            "prompt": "secret prompt",
            "job_id": "job-1",
            "note": "plain",
        }
        safe = redact_mapping(record)
        assert safe["transcript"] == digest_of({"redacted": "secret words"})
        assert safe["audio"] == sha256_bytes(b"\x00\x01")
        assert safe["prompt"] == digest_of({"redacted": "secret prompt"})
        assert safe["job_id"] == "job-1"
        assert safe["note"] == "plain"
        blob = str(safe)
        assert "secret words" not in blob
        assert "secret prompt" not in blob

    def test_original_record_is_not_mutated(self) -> None:
        record = {"transcript": "keep me"}
        redact_mapping(record)
        assert record["transcript"] == "keep me"


class TestPrivacyPolicy:
    def test_local_vault_is_default_and_refuses_egress(self) -> None:
        policy = PrivacyPolicy(now=NOW)
        policy.require_local(PrivacyMode.LOCAL_VAULT, "audio")  # no-op: nothing leaves
        with pytest.raises(PrivacyViolation) as exc:
            policy.check_egress(PrivacyMode.LOCAL_VAULT, "audio", consented=True)
        assert exc.value.reason == "egress_refused"

    def test_cloud_mode_requires_recorded_consent(self) -> None:
        policy = PrivacyPolicy(now=NOW)
        with pytest.raises(PrivacyViolation) as exc:
            policy.check_egress(PrivacyMode.TRUSTED_CLOUD, "transcript", consented=False)
        assert exc.value.reason == "consent_missing"

    def test_even_with_consent_no_provider_is_wired_so_egress_refuses(self) -> None:
        policy = PrivacyPolicy(now=NOW)
        policy.record_consent(PrivacyMode.TRUSTED_CLOUD, principal_id="u", scope="transcript")
        assert policy.has_consent(PrivacyMode.TRUSTED_CLOUD, principal_id="u", scope="transcript")
        with pytest.raises(PrivacyViolation) as exc:
            policy.check_egress(PrivacyMode.TRUSTED_CLOUD, "transcript", consented=True)
        assert exc.value.reason == "no_provider_wired"

    def test_consent_is_scoped(self) -> None:
        policy = PrivacyPolicy(now=NOW)
        policy.record_consent(PrivacyMode.EXPERIMENTAL_CLOUD, principal_id="u", scope="audio")
        assert not policy.has_consent(
            PrivacyMode.EXPERIMENTAL_CLOUD, principal_id="u", scope="transcript"
        )
        assert not policy.has_consent(
            PrivacyMode.EXPERIMENTAL_CLOUD, principal_id="other", scope="audio"
        )
