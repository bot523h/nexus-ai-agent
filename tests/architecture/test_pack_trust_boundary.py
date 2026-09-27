"""Architecture ratchets for the pack trust plane.

These are the guards that make the *next* change safe, not this one: they fail
if someone re-introduces a class of defect rather than a single instance.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent"
PACKS = SRC / "creative" / "packs"
TRUST = PACKS / "trust.py"
ED25519 = PACKS / "ed25519.py"
REGISTRY = PACKS / "registry.py"
VERIFY = PACKS / "verify.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in ast.walk(_tree(path)) if isinstance(node, ast.FunctionDef)}


def test_the_trust_path_ships_no_signing_primitive() -> None:
    """Production can *verify* authority; it must never be able to *mint* it."""
    for path in (TRUST, ED25519):
        source = path.read_text(encoding="utf-8")
        names = {node.name for node in ast.walk(_tree(path)) if isinstance(node, ast.FunctionDef)}
        assert not any(
            name == "sign" or name.startswith("sign_") or name.endswith("_sign") for name in names
        ), f"{path.name} defines a signing function"
        for forbidden in ("from_private_bytes", "SigningKey", "private_key", "generate("):
            assert forbidden not in source, f"{path.name} touches private key material"


def test_no_private_key_material_is_derivable_from_the_trust_path() -> None:
    """No checked-in 32-byte seed, and no deterministic derivation recipe."""
    for path in sorted(PACKS.glob("*.py")) + [PACKS / "trust_root.json"]:
        text = path.read_text(encoding="utf-8")
        for candidate in re.findall(r"\b[0-9a-fA-F]{64}\b", text):
            # Curve constants are decimal in this codebase; a 64-hex literal in
            # the trust path would be either a key or a seed.
            assert path.name == "ed25519.py" and candidate.startswith("d75a"), (
                f"{path.name} contains a 64-hex literal that could be key material: {candidate}"
            )


def test_activation_is_gated_on_the_trust_root() -> None:
    activate = _functions(REGISTRY)["activate"]
    used = {node.id for node in ast.walk(activate) if isinstance(node, ast.Name)}
    assert "TRUSTED_SIGNATURE_STATES" in used, (
        "PackRegistry.activate no longer consults TRUSTED_SIGNATURE_STATES — "
        "an external pack could become executable without a verified signature"
    )
    attrs = {node.attr for node in ast.walk(activate) if isinstance(node, ast.Attribute)}
    assert {"anchor", "signature_state"} <= attrs


def test_only_one_state_grants_trust() -> None:
    from nexus_ai_agent.creative.packs.trust import TRUSTED_STATES, TrustState
    from nexus_ai_agent.creative.packs.verify import TRUSTED_SIGNATURE_STATES

    assert TRUSTED_STATES == {TrustState.VERIFIED}
    assert TRUSTED_SIGNATURE_STATES == {"verified"}
    # Every other state must exist as its own, distinguishable outcome.
    assert len(set(TrustState)) >= 9


def test_there_is_exactly_one_canonicalisation_function() -> None:
    """Signer and verifier cannot drift if only one function produces bytes."""
    hits = [
        path
        for path in SRC.rglob("*.py")
        if "def canonical_signing_bytes" in path.read_text(encoding="utf-8")
    ]
    assert hits == [TRUST]


def test_verification_never_grants_trust_by_itself() -> None:
    source = VERIFY.read_text(encoding="utf-8")
    assert "def trusted" in source
    # `ok` (structure/policy) and `trusted` (authority) must stay separate.
    tree = _tree(VERIFY)
    report = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "VerificationReport"
    )
    props = {n.name for n in report.body if isinstance(n, ast.FunctionDef)}
    assert {"ok", "trusted"} <= props


def test_no_manifest_field_can_switch_the_verifier_off() -> None:
    """Grep-level guard: the trust decision never reads a manifest boolean."""
    source = TRUST.read_text(encoding="utf-8")
    for forbidden in (
        "manifest.security.trusted_publisher in",
        "skip_verification",
        "allow_unsigned",
        "trust_me",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(
    "module",
    ["nexus_ai_agent.creative.packs.trust", "nexus_ai_agent.creative.packs.ed25519"],
)
def test_trust_modules_import_without_optional_dependencies(module: str) -> None:
    import importlib

    importlib.import_module(module)


# --------------------------------------------------------------------------
# No duplicate authority (integration guard, ADR 0006 §"Duplicate authority")
# --------------------------------------------------------------------------

#: The delivery pack ships its own sign/verify seam
#: (``creative/packs/delivery/signing.py``) whose fallback is symmetric
#: HMAC-SHA256 under the *same* secret used to verify — a verifier there can
#: forge.  It is a transport seam for exported OTIO documents, not a trust
#: root, and it must never become one: pack *activation* authority has exactly
#: one source, :mod:`nexus_ai_agent.creative.packs.trust`.
DELIVERY_SIGNING = "nexus_ai_agent.creative.packs.delivery.signing"


def test_pack_trust_never_delegates_to_the_delivery_signing_seam() -> None:
    for path in (TRUST, ED25519, VERIFY, REGISTRY, PACKS / "manifest.py"):
        source = path.read_text(encoding="utf-8")
        assert "delivery.signing" not in source, (
            f"{path.name} reaches into the delivery signing seam; "
            "activation authority must come from the trust root alone"
        )
        assert "NEXUS_SIGNING_KEY" not in source


def test_the_delivery_seam_cannot_reach_the_trust_root() -> None:
    """Symmetry guard: the weaker seam must not import the stronger one either."""
    source = (PACKS / "delivery" / "signing.py").read_text(encoding="utf-8")
    for forbidden in ("packs.trust", "trust_root", "TRUSTED_SIGNATURE_STATES"):
        assert forbidden not in source


def test_only_the_trust_root_decides_pack_activation() -> None:
    """Repo-wide: nothing else may write a pack's ``active`` flag."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "RegisteredPack(" in path.read_text(encoding="utf-8") and path.name != "registry.py"
    ]
    assert offenders == [], f"{offenders} construct RegisteredPack outside the registry"


def test_the_activation_guard_has_a_positive_control() -> None:
    """A guard that can never fire is decoration: prove this one can."""
    hits = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "RegisteredPack(" in path.read_text(encoding="utf-8")
    ]
    assert hits == ["creative/packs/registry.py"]


# --------------------------------------------------------------------------
# Exactly one verifier (ADR 0006 §"Cryptography")
# --------------------------------------------------------------------------
# ``trust.py`` once advertised an optional ``cryptography`` backend that did not
# exist, and ``ed25519.py`` claimed that "both backends accept exactly the same
# set of signatures".  Both statements were false, and the second was the more
# dangerous: a maintainer who believes two backends agree will happily add the
# second one, and a second implementation would not carry the pinned edge-case
# rules (canonical ``S``, canonical point encodings, small-order rejection) —
# the small-order rule in particular is what stops an attacker-chosen key from
# verifying anything.  The claim is therefore pinned in code, not in prose.

#: Third-party crypto libraries a second verifier would plausibly be built on.
_OPTIONAL_CRYPTO_BACKENDS = ("cryptography", "nacl", "openssl", "ecdsa", "pynacl")


@pytest.mark.parametrize("path", [TRUST, ED25519], ids=lambda p: p.name)
def test_the_trust_path_imports_no_optional_crypto_backend(path: Path) -> None:
    """One verifier, no fallback.  A second backend is a security decision."""
    tree = _tree(path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    offenders = sorted(imported & set(_OPTIONAL_CRYPTO_BACKENDS))
    assert not offenders, (
        f"{path.name} imports {offenders}: a second verifier would not carry the "
        "pinned edge-case rules, and a 'library missing' path decays into "
        "'not verified, but allowed'"
    )


def test_the_shipped_verifier_backend_is_the_pure_python_one() -> None:
    """The one implementation must be the one that is actually used."""
    from nexus_ai_agent.creative.packs.ed25519 import BACKEND

    assert BACKEND == "pure-python"


#: Phrases that only make sense if a second verifier exists.
_FALSE_BACKEND_CLAIMS = (
    "both backends",
    "when it is installed",
    "accept exactly the same set",
)


def _false_backend_claims(source: str) -> list[str]:
    """Detector, kept pure so its red-proof can run on a fixture."""
    return [claim for claim in _FALSE_BACKEND_CLAIMS if claim in source]


def test_the_ed25519_module_does_not_claim_a_second_backend() -> None:
    """Doc-truth ratchet: the module must not describe a backend that is absent.

    The pre-fix module docstring said the cofactorless equation matched "what
    ``cryptography``/OpenSSL do — so both backends of this module accept exactly
    the same set of signatures".  There was no second backend, and the sets are
    not equal (small-order keys are refused here and accepted by OpenSSL).
    """
    found = _false_backend_claims(ED25519.read_text(encoding="utf-8"))
    assert not found, f"ed25519.py still claims {found}; there is exactly one backend"


def test_the_false_backend_claim_detector_can_actually_fire() -> None:
    """Red-proof: the ratchet above is worthless if the detector is inert."""
    historical = (
        "matching RFC 8032 section 5.1.7 and what ``cryptography``/OpenSSL do — "
        "so both backends of this module accept exactly the same set of signatures."
    )
    assert _false_backend_claims(historical) == [
        "both backends",
        "accept exactly the same set",
    ]
    assert _false_backend_claims("one implementation, always active") == []
