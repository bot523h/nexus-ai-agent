"""The historical provenance spelling is a frozen contract (JCS must not move it).

``provenance.models.canonical_json`` / ``digest_of`` (and the IBPC re-export)
pre-date RFC 8785 in this repository. The ledger, the Artifact Passport and the
IBPC approval/session digests all bind to *those* bytes, so they must stay
byte-for-byte identical — the JCS foundation is additive, not a migration.

The digests below were captured from the pre-change tree (``main`` @ the task
base) and are asserted as literals: if any future change re-spells the legacy
canonical form, this test goes red before a single stored hash is invalidated.
"""

from __future__ import annotations

from nexus_ai_agent.nagar.ibpc import canonical as ibpc_canonical
from nexus_ai_agent.provenance import models as provenance_models

# (value, legacy canonical bytes, legacy sha256:<hex> digest) — captured pre-change.
LEGACY_GOLDENS: list[tuple[object, str, str]] = [
    (
        {"b": 2, "a": 1},
        '{"a":1,"b":2}',
        "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
    ),
    (
        {"z": [1, 2, {"y": "x"}], "a": None},
        '{"a":null,"z":[1,2,{"y":"x"}]}',
        "sha256:b1275a9050920254157d06c13f1b65c36d2616e6dc265ee0c52d5dd1bc749047",
    ),
    (
        {"k": "café", "emoji": "😀"},
        '{"emoji":"😀","k":"café"}',
        "sha256:185007b8ee8dc1b3925f9f80f38f91563832ff9e5e0f90c9c348617e24c92ec0",
    ),
    (
        {"f": 1.5, "i": 3},
        '{"f":1.5,"i":3}',
        "sha256:e5020baaea584d942db52a0fe516122251d75d012df86083362aba8e49365984",
    ),
    (
        {},
        "{}",
        "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    ),
    (
        [1, "two", 3.0, True, False, None],
        '[1,"two",3.0,true,false,null]',
        "sha256:c1c11506a2d9371ede6983fe078615d889b6dd22bb2be28fada4dbf0a9458d16",
    ),
]


def test_legacy_canonical_bytes_are_unchanged() -> None:
    for value, expected_bytes, _ in LEGACY_GOLDENS:
        assert provenance_models.canonical_json(value) == expected_bytes


def test_legacy_digests_are_unchanged() -> None:
    for value, _, expected_digest in LEGACY_GOLDENS:
        assert provenance_models.digest_of(value) == expected_digest


def test_legacy_sha256_hex_spelling_is_unchanged() -> None:
    assert provenance_models.sha256_hex(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_ibpc_reexports_are_the_same_objects() -> None:
    """The IBPC canonical surface must still be the provenance surface."""
    assert ibpc_canonical.canonical_json is provenance_models.canonical_json
    assert ibpc_canonical.digest_of is provenance_models.digest_of
    assert ibpc_canonical.sha256_hex is provenance_models.sha256_hex


def test_legacy_and_jcs_are_distinct_spellings() -> None:
    """The two spellings legitimately differ; that is why JCS is additive.

    RFC 8785 pins ECMAScript number formatting and UTF-16 key ordering; the
    legacy ``sort_keys`` + ``json.dumps`` spelling uses Python's own number
    formatting and Unicode code-point ordering. A payload that exercises either
    therefore hashes differently under each spelling — this documents the
    divergence so nobody "fixes" it by wiring JCS into the legacy call sites.
    """
    from nexus_ai_agent.provenance.jcs import canonical_bytes

    numbers = {"x": 1e-7}
    assert provenance_models.canonical_json(numbers) == '{"x":1e-07}'
    assert canonical_bytes(numbers) == b'{"x":1e-7}'

    ordering = {"\ufb33": "Dalet", "\U0001f600": "Emoji"}
    # legacy sorts by code point (U+FB33 < U+1F600); JCS by UTF-16 code unit.
    assert provenance_models.canonical_json(ordering) == '{"\ufb33":"Dalet","\U0001f600":"Emoji"}'
    assert canonical_bytes(ordering) == b'{"\xf0\x9f\x98\x80":"Emoji","\xef\xac\xb3":"Dalet"}'
