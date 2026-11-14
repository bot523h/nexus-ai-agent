"""WAVE 32 — the Twin-Taxonomy Provisioner.

A capability only counts as delivered when it is *reachable* and *proven*. The
atlas therefore keeps two taxonomies that must mirror each other:

* the **source taxonomy** — the operator modules under ``creative/atlas/``;
* the **test taxonomy** — the evidence modules under ``tests/creative/atlas/``.

The provisioner pairs them into a **twin taxonomy** and refuses to bless a tree
in which a source module has no test (an unproven capability) or a test module
names no known source (a test with nothing to prove). It is deliberately
file-name driven and pure-stdlib: given the two module-name sets it derives the
pairing, and :func:`scan_python_taxonomy` supplies those sets from a real
directory when a caller wants the live tree.

The result is a canonical :class:`TwinTaxonomy` (JCS bytes + digest) that can be
sealed into the Wave 30 vault and appended to the Wave 31 ledger, so the proof
of "every capability is tested" travels with the same tamper-evident machinery
as the equations themselves.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from nexus_ai_agent.creative.atlas.atlas_spine_governance import jcs_canonical_bytes
from nexus_ai_agent.creative.atlas.holographic_equation_vault import HolographicEquationVault
from nexus_ai_agent.creative.atlas.self_healing_linear_ledger import LinearEvidenceLedger

__all__ = [
    "TAXONOMY_SCHEMA",
    "SOURCE_KIND",
    "TEST_KIND",
    "DEFAULT_MAX_TESTS_PER_SOURCE",
    "SOURCE_MODULE_PACKAGE",
    "TaxonomyError",
    "Taxon",
    "TwinPair",
    "TwinTaxonomy",
    "TwinTaxonomyReport",
    "SemanticTwinReport",
    "TwinTaxonomyProvisioner",
    "scan_python_taxonomy",
]

TAXONOMY_SCHEMA: Final[str] = "atlas.twin-taxonomy/1"
SOURCE_KIND: Final[str] = "source"
TEST_KIND: Final[str] = "test"
#: A source covered by more than this many tests is flagged as over-provisioned.
DEFAULT_MAX_TESTS_PER_SOURCE: Final[int] = 4
#: The dotted package the source taxonomy lives in; a twin test must import it.
SOURCE_MODULE_PACKAGE: Final[str] = "nexus_ai_agent.creative.atlas"


class TaxonomyError(ValueError):
    """Raised for a malformed taxonomy declaration or a failed pairing."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _stem(module_name: str) -> str:
    name = module_name
    if name.endswith(".py"):
        name = name[:-3]
    return name


def _as_sorted_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise TaxonomyError("taxonomy members must be non-empty strings")
        if value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(sorted(out))


@dataclass(frozen=True)
class Taxon:
    """One member of a taxonomy: a source operator or a test module."""

    taxon_id: str
    kind: str
    path: str
    role: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"taxon_id": self.taxon_id, "kind": self.kind, "path": self.path, "role": self.role}


@dataclass(frozen=True)
class TwinPair:
    """A source taxon and the test taxa that prove it."""

    source: Taxon
    tests: tuple[Taxon, ...]

    @property
    def twin_count(self) -> int:
        return len(self.tests)

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source.to_dict(), "tests": [t.to_dict() for t in self.tests]}


@dataclass(frozen=True)
class TwinTaxonomyReport:
    """The provisioning verdict for a source/test taxonomy pair."""

    source_count: int
    test_count: int
    paired_source_count: int
    untested_sources: tuple[str, ...]
    orphan_tests: tuple[str, ...]
    overprovisioned_sources: tuple[str, ...]

    @property
    def provisioned(self) -> bool:
        """True when every source is tested and every test names a source."""
        return not self.untested_sources and not self.orphan_tests

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_count": self.source_count,
            "test_count": self.test_count,
            "paired_source_count": self.paired_source_count,
            "untested_sources": list(self.untested_sources),
            "orphan_tests": list(self.orphan_tests),
            "overprovisioned_sources": list(self.overprovisioned_sources),
            "provisioned": self.provisioned,
        }


@dataclass(frozen=True)
class SemanticTwinReport:
    """The AST-level verdict: does each twin test *actually exercise* its source?

    A file-name match (``test_x.py`` for ``x.py``) is a naming convention, not
    evidence. This report parses each paired test module and requires it to
    import the source's module (``from nexus_ai_agent.creative.atlas import x``
    or an ``import nexus_ai_agent.creative.atlas.x``), so a hollow test that
    proves nothing is refused rather than blessed.
    """

    checked_pairs: int
    non_exercising_tests: tuple[str, ...]
    unreadable_tests: tuple[str, ...]

    @property
    def semantic_ok(self) -> bool:
        return not self.non_exercising_tests and not self.unreadable_tests

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_pairs": self.checked_pairs,
            "non_exercising_tests": list(self.non_exercising_tests),
            "unreadable_tests": list(self.unreadable_tests),
            "semantic_ok": self.semantic_ok,
        }


def _imported_atlas_modules(source: ast.Module) -> set[str]:
    """The atlas submodule stems a test module imports (directly or via ``from``)."""
    found: set[str] = set()
    for node in ast.walk(source):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == SOURCE_MODULE_PACKAGE:
                found.update(alias.name for alias in node.names)
            elif node.module.startswith(SOURCE_MODULE_PACKAGE + "."):
                found.add(node.module[len(SOURCE_MODULE_PACKAGE) + 1 :].split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(SOURCE_MODULE_PACKAGE + "."):
                    found.add(alias.name[len(SOURCE_MODULE_PACKAGE) + 1 :].split(".")[0])
    return found


def _exercises_source(test_path: Path, source_stem: str) -> bool:
    """Whether the test module at ``test_path`` imports the ``source_stem`` module."""
    try:
        source = ast.parse(test_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    return source_stem in _imported_atlas_modules(source)


@dataclass(frozen=True)
class TwinTaxonomy:
    """The provisioned twin taxonomy: canonical bytes, digest and sealing helpers."""

    pairs: tuple[TwinPair, ...]
    schema: str = TAXONOMY_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "pairs": [p.to_dict() for p in self.pairs],
        }

    def canonical_bytes(self) -> bytes:
        return jcs_canonical_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return _sha256(self.canonical_bytes())

    def seal_into_vault(self, vault: HolographicEquationVault) -> None:
        """Seal each pair's source→tests edge as a vault equation."""
        for pair in self.pairs:
            statement = "->".join([pair.source.taxon_id, *(t.taxon_id for t in pair.tests)])
            vault.seal(f"twin:{pair.source.taxon_id}", 32, statement)

    def append_to_ledger(self, ledger: LinearEvidenceLedger) -> None:
        """Append the canonical taxonomy to a self-healing ledger."""
        ledger.append(self.canonical_bytes())


class TwinTaxonomyProvisioner:
    """Pair a source taxonomy with a test taxonomy and prove the mirror.

    ``overrides`` maps a source stem to the extra test stems that prove it, for
    the cases where the ``test_<stem>`` convention does not hold.
    """

    def __init__(
        self,
        sources: Sequence[str],
        tests: Sequence[str],
        *,
        overrides: Mapping[str, Sequence[str]] | None = None,
        max_tests_per_source: int = DEFAULT_MAX_TESTS_PER_SOURCE,
        source_root: str = "src/nexus_ai_agent/creative/atlas",
        test_root: str = "tests/creative/atlas",
    ) -> None:
        if not isinstance(max_tests_per_source, int) or max_tests_per_source < 1:
            raise TaxonomyError("max_tests_per_source must be an integer >= 1")
        self._sources = _as_sorted_unique(sources)
        self._tests = _as_sorted_unique(tests)
        self._max_tests = max_tests_per_source
        self._source_root = source_root
        self._test_root = test_root
        self._overrides: dict[str, tuple[str, ...]] = {}
        for source, extra in (overrides or {}).items():
            self._overrides[_stem(source)] = _as_sorted_unique(extra)

    @classmethod
    def from_workspace(
        cls,
        source_dir: str | Path,
        test_dir: str | Path,
        *,
        overrides: Mapping[str, Sequence[str]] | None = None,
        max_tests_per_source: int = DEFAULT_MAX_TESTS_PER_SOURCE,
    ) -> TwinTaxonomyProvisioner:
        """Provision a twin taxonomy for a real workspace directory pair.

        Scans ``source_dir`` and ``test_dir`` for their module stems (skipping
        ``__init__`` and private helpers) and returns a provisioner bound to the
        *actual* tree, so the taxonomy reflects the shipped files rather than a
        caller-supplied name list.
        """
        sources = scan_python_taxonomy(source_dir)
        tests = scan_python_taxonomy(test_dir)
        return cls(
            sources,
            tests,
            overrides=overrides,
            max_tests_per_source=max_tests_per_source,
            source_root=str(source_dir),
            test_root=str(test_dir),
        )

    @property
    def sources(self) -> tuple[str, ...]:
        return self._sources

    @property
    def tests(self) -> tuple[str, ...]:
        return self._tests

    def _test_pool(self) -> set[str]:
        return {_stem(t) for t in self._tests}

    def _tests_for(self, source_stem: str, pool: set[str]) -> tuple[str, ...]:
        candidates = {f"test_{source_stem}", *self._overrides.get(source_stem, ())}
        matched = {c for c in candidates if c in pool}
        return tuple(sorted(matched))

    def provision(self) -> TwinTaxonomy:
        """Derive the twin taxonomy; raise if a source is untested."""
        pool = self._test_pool()
        pairs: list[TwinPair] = []
        for source in self._sources:
            stem = _stem(source)
            matched = self._tests_for(stem, pool)
            if not matched:
                raise TaxonomyError(f"source {source!r} has no twin test (untested capability)")
            source_taxon = Taxon(
                taxon_id=stem, kind=SOURCE_KIND, path=f"{self._source_root}/{stem}.py"
            )
            test_taxa = tuple(
                Taxon(taxon_id=name, kind=TEST_KIND, path=f"{self._test_root}/{name}.py")
                for name in matched
            )
            pairs.append(TwinPair(source=source_taxon, tests=test_taxa))
        return TwinTaxonomy(pairs=tuple(pairs))

    def report(self) -> TwinTaxonomyReport:
        """Classify the pairing without raising on an untested source."""
        pool = self._test_pool()
        untested: list[str] = []
        overprovisioned: list[str] = []
        paired = 0
        matched_tests: set[str] = set()
        for source in self._sources:
            stem = _stem(source)
            matched = self._tests_for(stem, pool)
            if not matched:
                untested.append(stem)
                continue
            paired += 1
            matched_tests.update(matched)
            if len(matched) > self._max_tests:
                overprovisioned.append(stem)
        orphan = tuple(sorted(pool - matched_tests))
        return TwinTaxonomyReport(
            source_count=len(self._sources),
            test_count=len(self._tests),
            paired_source_count=paired,
            untested_sources=tuple(sorted(untested)),
            orphan_tests=orphan,
            overprovisioned_sources=tuple(sorted(overprovisioned)),
        )

    def verify_semantic(self) -> SemanticTwinReport:
        """AST-verify that every paired twin test actually imports its source.

        Reads each paired test file under ``test_root`` and requires it to import
        the source module; a name-only pairing that never touches the source is
        reported as non-exercising, so the twin taxonomy cannot be satisfied by
        an empty test file that merely shares a stem.
        """
        taxonomy = self.provision()
        non_exercising: list[str] = []
        unreadable: list[str] = []
        for pair in taxonomy.pairs:
            for test in pair.tests:
                test_path = Path(self._test_root) / f"{test.taxon_id}.py"
                if not test_path.is_file():
                    unreadable.append(test.taxon_id)
                    continue
                if not _exercises_source(test_path, pair.source.taxon_id):
                    non_exercising.append(test.taxon_id)
        return SemanticTwinReport(
            checked_pairs=len(taxonomy.pairs),
            non_exercising_tests=tuple(sorted(non_exercising)),
            unreadable_tests=tuple(sorted(unreadable)),
        )


def scan_python_taxonomy(root: str | Path) -> tuple[str, ...]:
    """Return the module stems of every ``*.py`` file directly under ``root``.

    ``__init__`` and private helpers (``_*.py``) are excluded: they are not
    standalone capabilities and do not need a twin test of their own.
    """
    path = Path(root)
    if not path.is_dir():
        raise TaxonomyError(f"{root!r} is not a directory")
    stems = [
        child.stem
        for child in sorted(path.glob("*.py"))
        if child.stem != "__init__" and not child.stem.startswith("_")
    ]
    return tuple(stems)
