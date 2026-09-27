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
