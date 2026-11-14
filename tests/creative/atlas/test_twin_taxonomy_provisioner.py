"""WAVE 32 — tests for the Twin-Taxonomy Provisioner.

Covers stem normalisation, the source→test pairing convention (with
overrides), the provisioned/refused verdicts, the canonical taxonomy digest,
the sealing helpers into the Wave 30 vault and Wave 31 ledger, and the live
directory scanner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.creative.atlas import holographic_equation_vault as hv
from nexus_ai_agent.creative.atlas import self_healing_linear_ledger as shl
from nexus_ai_agent.creative.atlas import twin_taxonomy_provisioner as tt

SOURCES = ["alpha", "beta", "gamma"]
TESTS = ["test_alpha", "test_beta", "test_gamma"]


def _provisioner(**kwargs: object) -> tt.TwinTaxonomyProvisioner:
    return tt.TwinTaxonomyProvisioner(SOURCES, TESTS, **kwargs)  # type: ignore[arg-type]


# --- pairing ---------------------------------------------------------------------


def test_pairing_follows_the_convention() -> None:
    taxonomy = _provisioner().provision()
    assert [p.source.taxon_id for p in taxonomy.pairs] == SOURCES
    for pair in taxonomy.pairs:
        assert pair.twin_count == 1
        assert pair.tests[0].taxon_id == f"test_{pair.source.taxon_id}"
        assert pair.source.kind == tt.SOURCE_KIND
        assert pair.tests[0].kind == tt.TEST_KIND


def test_overrides_pair_a_non_conventional_test() -> None:
    provisioner = tt.TwinTaxonomyProvisioner(
        ["alpha"],
        ["test_alpha_suite", "test_alpha_edges"],
        overrides={"alpha": ["test_alpha_suite", "test_alpha_edges"]},
    )
    pair = provisioner.provision().pairs[0]
    assert [t.taxon_id for t in pair.tests] == ["test_alpha_edges", "test_alpha_suite"]


def test_provision_refuses_an_untested_source() -> None:
    provisioner = tt.TwinTaxonomyProvisioner(["alpha", "delta"], TESTS)
    with pytest.raises(tt.TaxonomyError):
        provisioner.provision()


def test_report_flags_untested_sources_and_orphan_tests() -> None:
    provisioner = tt.TwinTaxonomyProvisioner(["alpha", "delta"], ["test_alpha", "test_mystery"])
    report = provisioner.report()
    assert report.provisioned is False
    assert report.untested_sources == ("delta",)
    assert report.orphan_tests == ("test_mystery",)
    assert report.paired_source_count == 1


def test_report_is_provisioned_for_a_clean_pair() -> None:
    report = _provisioner().report()
    assert report.provisioned is True
    assert report.untested_sources == ()
    assert report.orphan_tests == ()
    assert report.source_count == 3
    assert report.test_count == 3


def test_report_flags_overprovisioned_sources() -> None:
    provisioner = tt.TwinTaxonomyProvisioner(
        ["alpha"],
        ["test_alpha", "test_alpha_extra"],
        overrides={"alpha": ["test_alpha", "test_alpha_extra"]},
        max_tests_per_source=1,
    )
    assert provisioner.report().overprovisioned_sources == ("alpha",)


def test_constructor_validates_arguments() -> None:
    with pytest.raises(tt.TaxonomyError):
        tt.TwinTaxonomyProvisioner(SOURCES, TESTS, max_tests_per_source=0)
    with pytest.raises(tt.TaxonomyError):
        tt.TwinTaxonomyProvisioner(["alpha", ""], TESTS)
    with pytest.raises(tt.TaxonomyError):
        tt.TwinTaxonomyProvisioner([1], TESTS)  # type: ignore[list-item]


def test_duplicate_members_are_collapsed() -> None:
    provisioner = tt.TwinTaxonomyProvisioner(["alpha", "alpha"], ["test_alpha", "test_alpha"])
    assert provisioner.sources == ("alpha",)
    assert provisioner.tests == ("test_alpha",)


def test_module_stem_strips_a_py_suffix() -> None:
    assert tt._stem("alpha.py") == "alpha"
    assert tt._stem("alpha") == "alpha"


def test_scan_python_taxonomy_skips_dunder_and_private(tmp_path: object) -> None:
    root = tmp_path / "tax"  # type: ignore[operator]
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "_private.py").write_text("", encoding="utf-8")
    (root / "operator.py").write_text("", encoding="utf-8")
    (root / "notes.txt").write_text("", encoding="utf-8")
    assert tt.scan_python_taxonomy(root) == ("operator",)
    with pytest.raises(tt.TaxonomyError):
        tt.scan_python_taxonomy(root / "missing")


# --- canonical taxonomy + sealing -------------------------------------------------


def test_taxonomy_canonical_bytes_and_digest_are_deterministic() -> None:
    first = _provisioner().provision()
    second = _provisioner().provision()
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest == second.digest
    assert first.to_dict()["schema"] == tt.TAXONOMY_SCHEMA


def test_taxonomy_seals_into_the_vault() -> None:
    taxonomy = _provisioner().provision()
    vault = hv.HolographicEquationVault(k=2, parity=1)
    taxonomy.seal_into_vault(vault)
    assert {r.equation_id for r in vault.equations} == {
        "twin:alpha",
        "twin:beta",
        "twin:gamma",
    }
    assert vault.audit().all_verified is True


def test_taxonomy_appends_to_the_ledger() -> None:
    taxonomy = _provisioner().provision()
    ledger = shl.LinearEvidenceLedger(k=2, parity=1)
    taxonomy.append_to_ledger(ledger)
    assert len(ledger) == 1
    assert ledger.payload(0) == taxonomy.canonical_bytes()
    assert ledger.verify().intact is True


def test_report_and_pair_serialise() -> None:
    taxonomy = _provisioner().provision()
    assert set(taxonomy.pairs[0].to_dict()) == {"source", "tests"}
    assert set(taxonomy.pairs[0].source.to_dict()) == {"taxon_id", "kind", "path", "role"}
    report = _provisioner().report()
    assert set(report.to_dict()) == {
        "source_count",
        "test_count",
        "paired_source_count",
        "untested_sources",
        "orphan_tests",
        "overprovisioned_sources",
        "provisioned",
    }


def test_public_surface_matches_declared_exports() -> None:
    for name in tt.__all__:
        assert hasattr(tt, name), name


# --- semantic (AST) twin verification (Wave 34 hardening) ---------------------------


def _write_pair(root: Path, source_stem: str, test_body: str) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "src" / f"{source_stem}.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / f"test_{source_stem}.py").write_text(test_body, encoding="utf-8")


def test_verify_semantic_accepts_a_test_that_imports_its_source(tmp_path: Path) -> None:
    body = (
        "from nexus_ai_agent.creative.atlas import alpha\n\n\n"
        "def test_x():\n    assert alpha.VALUE == 1\n"
    )
    _write_pair(tmp_path, "alpha", body)
    provisioner = tt.TwinTaxonomyProvisioner.from_workspace(tmp_path / "src", tmp_path / "tests")
    report = provisioner.verify_semantic()
    assert report.semantic_ok is True
    assert report.checked_pairs == 1


def test_verify_semantic_refuses_a_hollow_name_only_test(tmp_path: Path) -> None:
    _write_pair(tmp_path, "alpha", "def test_nothing():\n    assert True\n")
    provisioner = tt.TwinTaxonomyProvisioner.from_workspace(tmp_path / "src", tmp_path / "tests")
    report = provisioner.verify_semantic()
    assert report.semantic_ok is False
    assert report.non_exercising_tests == ("test_alpha",)


def test_verify_semantic_accepts_a_dotted_import(tmp_path: Path) -> None:
    _write_pair(
        tmp_path,
        "beta",
        "import nexus_ai_agent.creative.atlas.beta as beta\n",
    )
    provisioner = tt.TwinTaxonomyProvisioner.from_workspace(tmp_path / "src", tmp_path / "tests")
    assert provisioner.verify_semantic().semantic_ok is True


def test_verify_semantic_reports_an_unreadable_test(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "from nexus_ai_agent.creative.atlas import alpha\n", encoding="utf-8"
    )
    provisioner = tt.TwinTaxonomyProvisioner(
        ["alpha"], ["test_alpha"], test_root=str(tmp_path / "missing")
    )
    report = provisioner.verify_semantic()
    assert report.semantic_ok is False
    assert report.unreadable_tests == ("test_alpha",)


def test_imported_atlas_modules_ignores_unrelated_imports() -> None:
    import ast

    module = ast.parse("import os\nfrom collections import OrderedDict\n")
    assert tt._imported_atlas_modules(module) == set()


def test_semantic_twin_report_serialises() -> None:
    report = tt.SemanticTwinReport(
        checked_pairs=2, non_exercising_tests=("test_x",), unreadable_tests=()
    )
    assert report.to_dict() == {
        "checked_pairs": 2,
        "non_exercising_tests": ["test_x"],
        "unreadable_tests": [],
        "semantic_ok": False,
    }


# --- workspace provisioning (Wave 34 hardening) ------------------------------------


def test_from_workspace_scans_the_real_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "_private.py").write_text("y = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_alpha.py").write_text("x = 1\n", encoding="utf-8")
    provisioner = tt.TwinTaxonomyProvisioner.from_workspace(tmp_path / "src", tmp_path / "tests")
    assert provisioner.sources == ("alpha",)
    assert provisioner.tests == ("test_alpha",)
    assert provisioner.report().provisioned is True


def test_imported_atlas_modules_reads_a_dotted_from_import() -> None:
    import ast

    module = ast.parse("from nexus_ai_agent.creative.atlas.alpha.beta import thing\n")
    assert tt._imported_atlas_modules(module) == {"alpha"}


def test_exercises_source_returns_false_for_unparsable_source(tmp_path: Path) -> None:
    broken = tmp_path / "test_broken.py"
    broken.write_text("def oops(:\n", encoding="utf-8")
    assert tt._exercises_source(broken, "broken") is False


def test_exercises_source_returns_false_for_a_missing_file(tmp_path: Path) -> None:
    assert tt._exercises_source(tmp_path / "nope.py", "nope") is False
