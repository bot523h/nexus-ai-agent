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

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from nexus_ai_agent.creative.atlas.atlas_spine_governance import jcs_canonical_bytes
from nexus_ai_agent.creative.atlas.holographic_equation_vault import HolographicEquationVault
from nexus_ai_agent.creative.atlas.self_healing_linear_ledger import SelfHealingLinearLedger

__all__ = [
    "TAXONOMY_SCHEMA",
    "SOURCE_KIND",
    "TEST_KIND",
    "DEFAULT_MAX_TESTS_PER_SOURCE",
    "TaxonomyError",
    "Taxon",
    "TwinPair",
    "TwinTaxonomy",
    "TwinTaxonomyReport",
    "TwinTaxonomyProvisioner",
    "scan_python_taxonomy",
]

TAXONOMY_SCHEMA: Final[str] = "atlas.twin-taxonomy/1"
SOURCE_KIND: Final[str] = "source"
TEST_KIND: Final[str] = "test"
#: A source covered by more than this many tests is flagged as over-provisioned.
DEFAULT_MAX_TESTS_PER_SOURCE: Final[int] = 4


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

    def append_to_ledger(self, ledger: SelfHealingLinearLedger) -> None:
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
