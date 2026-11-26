"""Architecture guard: JCS is additive and must never replace the legacy spelling.

RFC 8785 (``provenance.jcs``) and the repository's historical canonical JSON
(``provenance.models.canonical_json`` / ``digest_of``, re-exported by IBPC) are
**different** spellings that produce different bytes for some inputs. The
historical hashes are a frozen contract; wiring JCS into a legacy call site
would silently invalidate every stored ledger, passport and approval digest.

This guard is AST-based (it inspects imports, not text), so a comment mentioning
"JCS" cannot satisfy it and an import cannot hide behind formatting:

* the only modules allowed to reference the JCS surface are
  ``provenance/jcs.py`` (the definition), ``provenance/__init__.py`` (the
  re-export), ``provenance/canonicalization.py`` (the scheme dispatcher) and one
  explicitly reviewed *new* consumer, ``nagar/ibpc/approval.py`` (the Wave 3
  canonical approval binding: a brand-new, domain-separated HMAC over RFC 8785
  bytes that replaces no legacy digest — see
  ``test_ibpc_approval_binding_is_a_carefully_scoped_jcs_consumer``);
* the frozen legacy modules must not reference the JCS names at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "nexus_ai_agent"

#: Names that exist only on the JCS surface.
JCS_NAMES = {"jcs", "canonical_bytes", "canonical_text", "jcs_digest", "JcsError"}

#: Modules permitted to import the JCS surface.
ALLOWED_JCS_IMPORTERS = {
    Path("provenance/jcs.py"),
    Path("provenance/__init__.py"),
    Path("provenance/canonicalization.py"),
    # Wave 3 approval bindings are a NEW (schema v2) cryptographic contract:
    # they sign RFC 8785 bytes with a keyed HMAC under their own domain prefix.
    # No legacy digest is re-spelled and no frozen ledger is re-hashed, so this
    # consumer is additive by construction — the test below keeps it narrow.
    Path("nagar/ibpc/approval.py"),
}

#: The only JCS names the approved IBPC consumer may touch.
APPROVAL_JCS_NAMES = {"canonical_bytes", "CanonicalizationScheme"}

#: Frozen legacy modules whose hashes must not move.
FROZEN_LEGACY_MODULES = [
    Path("provenance/models.py"),
    Path("provenance/passport.py"),
    Path("provenance/journal.py"),
    Path("jobs/creative_passport.py"),
    Path("nagar/ibpc/canonical.py"),
]


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
    return names


def _importers_of_jcs() -> dict[Path, set[str]]:
    found: dict[Path, set[str]] = {}
    for path in SRC.rglob("*.py"):
        names = _imported_names(path)
        hits = names & JCS_NAMES
        module_tail = {name.rsplit(".", 1)[-1] for name in names}
        if hits or "provenance.jcs" in names or "rfc8785" in names:
            found[path.relative_to(SRC)] = hits | (module_tail & JCS_NAMES)
    return found


def test_only_the_jcs_surface_imports_jcs() -> None:
    offenders = {
        path: sorted(names)
        for path, names in _importers_of_jcs().items()
        if path not in ALLOWED_JCS_IMPORTERS
    }
    assert not offenders, (
        "JCS must stay additive: only provenance/jcs.py and provenance/__init__.py "
        f"may import it, found {offenders}"
    )


def test_frozen_legacy_modules_never_reference_jcs() -> None:
    violations: list[str] = []
    for rel in FROZEN_LEGACY_MODULES:
        path = SRC / rel
        assert path.exists(), f"frozen legacy module missing: {rel}"
        names = _imported_names(path)
        if names & JCS_NAMES or "rfc8785" in names:
            violations.append(str(rel))
    assert not violations, (
        "the historical canonical spelling is a frozen contract; these modules "
        f"must not reference JCS: {violations}"
    )


def test_legacy_canonical_json_keeps_its_sort_keys_spelling() -> None:
    """The legacy helper's implementation contract, asserted structurally."""
    tree = ast.parse((SRC / "provenance" / "models.py").read_text(encoding="utf-8"))
    canonical_json = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "canonical_json"
    )
    keywords = {
        kw.arg
        for node in ast.walk(canonical_json)
        if isinstance(node, ast.Call)
        for kw in node.keywords
    }
    assert {"sort_keys", "ensure_ascii", "separators"} <= keywords, (
        "provenance.models.canonical_json must keep sort_keys/ensure_ascii/"
        "separators — its bytes are a stored-hash contract"
    )


def test_ibpc_approval_binding_is_a_carefully_scoped_jcs_consumer() -> None:
    """The one non-provenance JCS consumer must stay minimal and indirect."""
    path = SRC / "nagar" / "ibpc" / "approval.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    # it may only reach JCS through the scheme dispatcher, never the raw module
    assert "nexus_ai_agent.provenance.jcs" not in modules
    assert imported & JCS_NAMES <= APPROVAL_JCS_NAMES
    # and it must not import any digest helper that would re-spell a legacy hash
    assert not {"jcs_digest", "jcs", "JcsError"} & imported
