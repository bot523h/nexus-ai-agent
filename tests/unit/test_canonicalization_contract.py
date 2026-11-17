import pytest

from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme as Scheme,
)
from nexus_ai_agent.provenance.canonicalization import (
    canonical_digest,
    verify_canonical_digest,
)
from nexus_ai_agent.provenance.jcs import JcsError, canonical_text, jcs_digest


def test_rfc8785_number_and_key_order_contract() -> None:
    assert canonical_text(-0.0) == "0"
    assert canonical_text(1.0) == "1"
    # UTF-16 ordering places the astral character before the private-use key.
    assert canonical_text({"\ue000": 1, "😀": 2}) == '{"😀":2,"":1}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_rfc8785_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(JcsError):
        canonical_text(value)


def test_versioned_legacy_and_jcs_digests_and_migration_fallback() -> None:
    payload = {"n": 1.0, "text": "nexus"}
    old = canonical_digest(payload, Scheme.LEGACY_JSON_V1)
    new = canonical_digest(payload, Scheme.JCS_RFC8785_V2)
    assert verify_canonical_digest(payload, old, Scheme.LEGACY_JSON_V1)
    assert verify_canonical_digest(payload, new, Scheme.JCS_RFC8785_V2)
    assert verify_canonical_digest(payload, old, migration_mode=True)
    assert verify_canonical_digest(payload, new, migration_mode=True)
    assert not verify_canonical_digest({**payload, "n": 2}, old, Scheme.LEGACY_JSON_V1)
    assert not verify_canonical_digest({**payload, "n": 2}, new, Scheme.JCS_RFC8785_V2)
    assert jcs_digest(payload) == new
