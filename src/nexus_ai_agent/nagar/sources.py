"""The three allowed sources of Nagar operation truth, read independently.

Nothing in this module reads a projection.  Every function here goes to a
*source*: the catalogue document, the live registry object, or the live
surface code.  That is what makes the gate in :mod:`nexus_ai_agent.nagar.truth`
non-self-referential — and what makes the mutation probes in
``tests/unit/test_operation_truth_mutations.py`` able to turn it red.

Source 1 — Product Catalog (``docs/NAGAR_70_OPERATIONS_TDD.md``)
----------------------------------------------------------------
``T01..T70`` are the rows of the **seven pack tables** of that document, in
document order.  Gate 4 engineered this rule because no file in the repository
defined the ids; the parser below reproduces it exactly, so the id mapping can
never drift from the catalogue it claims to describe.

Source 2 — Live Runtime Registry (``build_runtime_registry()``)
---------------------------------------------------------------
The composed registry is the runtime's own answer to "which operations can this
process dispatch".  Its size is a *measurement*, never a constant.

Source 3 — Live Executable Surface
----------------------------------
Three mechanically independent probes, which must agree:

``surface_allow_list``
    ``CreativeSurfaceMapper.ALLOWED`` — what the surface itself accepts.
``worker_closed_set``
    ``SURFACE_TO_CANONICAL`` — what the worker will dispatch, or die.
``slideshow_entrypoint``
    the canonical operations reached from the Telegram ``/slideshow`` handler
    through ``render_from_files``, derived by parsing the dispatch call sites.

Disagreement between the first two is a *contract drift*, not a detail: the
surface would accept a request the worker refuses, or drop one it could serve.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nexus_ai_agent.application.ports.job_queue import JobStatus

#: Repository root, derived from this file's location (``src/nexus_ai_agent/nagar``).
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The product catalogue: Nagar's 70-operation TDD.
CATALOG_PATH = Path("docs") / "NAGAR_70_OPERATIONS_TDD.md"

#: A pack table is introduced by this exact header cell.
_TABLE_HEADER = "| Operation"

#: A catalogue row is ``| `operation.id` | …`` (backticked id in the first cell).
_OPERATION_ROW = re.compile(r"\|\s*`([a-z][a-z0-9_]*\.[a-z0-9_]+)`")

#: Where the ``/slideshow`` Telegram handler's dispatch chain lives.
SLIDESHOW_SERVICE = Path("src") / "nexus_ai_agent" / "creative" / "slideshow" / "service.py"

#: The worker-side closed set for the ``creative_render`` lane.
RENDER_JOBS = Path("src") / "nexus_ai_agent" / "creative" / "render_jobs.py"

#: Gate 4's cross-layer evidence, consumed (never re-derived) by Gate 2.2.
GATE4_EVIDENCE = Path("docs") / "audits" / "GATE4_TRUTH_MATRIX.json"

#: The studio core registry: the operations that belong to no pack manifest.
WAVE1_REGISTRY_SOURCE = "nexus_ai_agent.creative.studio.capabilities.build_wave1_registry()"


class SourceError(RuntimeError):
    """A source could not be read, or did not have the shape the gate requires."""


# --------------------------------------------------------------------------- #
# Source 1 — Product Catalog
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CatalogRow:
    """One catalogue row: its id, the pack table it lives in, and its position."""

    ordinal: int
    operation_id: str
    category: str
    line_number: int

    @property
    def t_id(self) -> str:
        """``T01``-style id, zero-padded, in document order."""
        return f"T{self.ordinal:02d}"


@dataclass(frozen=True)
class Catalog:
    """The product catalogue as measured, plus the tables it was parsed from."""

    rows: tuple[CatalogRow, ...]
    table_lines: tuple[int, ...]

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(row.operation_id for row in self.rows)

    @property
    def by_t_id(self) -> dict[str, str]:
        return {row.t_id: row.operation_id for row in self.rows}

    @property
    def by_id(self) -> dict[str, CatalogRow]:
        return {row.operation_id: row for row in self.rows}


def _catalog_lines(root: Path) -> list[str]:
    path = root / CATALOG_PATH
    if not path.is_file():
        raise SourceError(f"product catalogue missing: {path}")
    return path.read_text(encoding="utf-8").splitlines()


def read_product_catalog(root: Path | None = None) -> Catalog:
    """Parse the seven pack tables of the TDD into an ordered catalogue.

    The parser is deliberately strict about *table membership*: a backticked
    ``a.b`` token outside a pack table (a prose example, a package id) is not a
    catalogue row, which is why the earlier naive ``grep`` over the same file
    reported 71 ids and this reports 70.
    """
    base = root if root is not None else REPO_ROOT
    lines = _catalog_lines(base)

    rows: list[CatalogRow] = []
    table_lines: list[int] = []
    inside_table: list[str] | None = None

    for line_number, line in enumerate(lines, start=1):
        if line.startswith(_TABLE_HEADER):
            table_lines.append(line_number)
            inside_table = []
            continue
        if inside_table is None:
            continue
        if not line.startswith("|"):
            inside_table = None
            continue
        match = _OPERATION_ROW.match(line)
        if match is None:
            continue
        operation_id = match.group(1)
        inside_table.append(operation_id)
        rows.append(
            CatalogRow(
                ordinal=len(rows) + 1,
                operation_id=operation_id,
                category=operation_id.split(".", 1)[0],
                line_number=line_number,
            )
        )

    if not rows:
        raise SourceError(f"no pack tables found in {CATALOG_PATH}")
    return Catalog(rows=tuple(rows), table_lines=tuple(table_lines))


# --------------------------------------------------------------------------- #
# Source 2 — Live Runtime Registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RuntimeOperation:
    """One operation the live runtime can dispatch, with its registered facts."""

    operation_id: str
    permission_level: str
    input_model: str
    input_forbids_extra: bool
    handler_name: str
    handler_is_named: bool
    deterministic: bool
    required_packs: tuple[str, ...]

    @property
    def domain_ready(self) -> bool:
        """A real, typed, non-stub domain operation.

        Three mechanical facts, each independently checkable: the registry
        holds a typed input model that forbids unknown fields, the model is
        attached to a *named* handler (a lambda would hide a placeholder), and
        the operation declares itself deterministic.
        """
        return self.input_forbids_extra and self.handler_is_named and self.deterministic

    @property
    def registrar(self) -> str:
        """Where the operation came from: a pack manifest, or the Wave-1 core.

        The mapping is a *measurement*, not a guess: the operations with no
        ``required_packs`` are exactly the ones ``build_wave1_registry()``
        registers, a fact
        ``tests/unit/test_operation_truth_sources.py::test_wave1_core_operations_are_exactly_the_pack_less_ones``
        re-checks on every run.
        """
        if self.required_packs:
            return ",".join(self.required_packs)
        return WAVE1_REGISTRY_SOURCE


@dataclass(frozen=True)
class Runtime:
    """The composed runtime registry as measured."""

    operations: tuple[RuntimeOperation, ...]

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(operation.operation_id for operation in self.operations)

    @property
    def by_id(self) -> dict[str, RuntimeOperation]:
        return {operation.operation_id: operation for operation in self.operations}

    @property
    def composition_issues(self) -> tuple[str, ...]:
        """Findings that make the pack composition inconsistent (empty = clean)."""
        from nexus_ai_agent.creative.packs.runtime import composition_issues

        return composition_issues()


def read_runtime_registry() -> Runtime:
    """Read the live composed registry — the runtime's own answer."""
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    registry = build_runtime_registry()
    operations: list[RuntimeOperation] = []
    for operation_id in sorted(registry.list_operations()):
        spec = registry.get_spec(operation_id)
        handler = spec.handler
        handler_name = getattr(handler, "__name__", "") or type(handler).__name__
        named = bool(handler_name) and handler_name != "<lambda>"
        operations.append(
            RuntimeOperation(
                operation_id=operation_id,
                permission_level=str(spec.permission_level.value),
                input_model=f"{spec.input_model.__module__}.{spec.input_model.__qualname__}",
                input_forbids_extra=(spec.input_model.model_config or {}).get("extra") == "forbid",
                handler_name=handler_name,
                handler_is_named=callable(handler) and named,
                deterministic=bool(spec.deterministic),
                required_packs=tuple(spec.required_packs),
            )
        )
    return Runtime(operations=tuple(operations))


# --------------------------------------------------------------------------- #
# Source 3 — Live Executable Surface
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SurfaceProbe:
    """One independent probe of the executable surface.

    ``unresolved`` is the interesting field: a pair the surface accepts but the
    worker's closed map cannot name is exactly the "surface accepts what nothing
    can execute" defect, and it is recorded rather than dropped.
    """

    name: str
    pairs: tuple[tuple[str, str], ...]
    operations: tuple[str, ...]
    source: str
    unresolved: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Surface:
    """The executable surface, as three independent probes measured it."""

    allow_list: SurfaceProbe
    worker_closed_set: SurfaceProbe
    slideshow_entrypoint: SurfaceProbe
    commands: tuple[str, ...]

    @property
    def pairs(self) -> frozenset[tuple[str, str]]:
        """The ``(command, operation)`` requests the surface accepts and serves."""
        return frozenset(self.allow_list.pairs)

    @property
    def operation_ids(self) -> tuple[str, ...]:
        """Every canonical operation reachable from a live user-facing entrypoint.

        This is the union of the ``/edit``, ``/caption``, ``/grade`` surface and
        the ``/slideshow`` surface — a *derived* set, never a hand-written
        constant.  It is deliberately larger than the worker's closed set: the
        slideshow reaches operations through its own render path.
        """
        return tuple(
            sorted(set(self.allow_list.operations) | set(self.slideshow_entrypoint.operations))
        )

    @property
    def disagreements(self) -> tuple[str, ...]:
        """Ways the probes contradict each other (empty = they agree)."""
        findings: list[str] = []
        if self.pairs != frozenset(self.worker_closed_set.pairs):
            only_allow = sorted(self.pairs - frozenset(self.worker_closed_set.pairs))
            only_worker = sorted(frozenset(self.worker_closed_set.pairs) - self.pairs)
            findings.append(
                "surface allow-list and worker closed set disagree: "
                f"surface-only={only_allow} worker-only={only_worker}"
            )
        if self.allow_list.unresolved:
            findings.append(
                "surface accepts requests nothing can execute: "
                f"{sorted(self.allow_list.unresolved)}"
            )
        if set(self.allow_list.operations) != set(self.worker_closed_set.operations):
            divergent = sorted(
                set(self.allow_list.operations) ^ set(self.worker_closed_set.operations)
            )
            findings.append(
                "surface allow-list and worker closed set map to different canonical "
                f"operations: {divergent}"
            )
        if not self.commands:
            findings.append("the surface registers no command")
        return tuple(findings)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.AST:
    return ast.parse(_read(path), filename=str(path))


def probe_surface_allow_list() -> SurfaceProbe:
    """Probe 1: what the surface's own mapper accepts.

    The mapper is a pure validator: it knows ``(command, operation)`` pairs and
    nothing about canonical ids, so the probe resolves each accepted pair
    through the worker map and records any pair it cannot resolve.
    """
    from nexus_ai_agent.bot.creative_surface import CreativeSurfaceMapper
    from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL

    pairs = tuple(
        sorted(
            (command, operation)
            for command, operations in CreativeSurfaceMapper.ALLOWED.items()
            for operation in operations
        )
    )
    operations: list[str] = []
    unresolved: list[tuple[str, str]] = []
    for pair in pairs:
        canonical = SURFACE_TO_CANONICAL.get(pair)
        if canonical is None:
            unresolved.append(pair)
        else:
            operations.append(canonical)
    return SurfaceProbe(
        name="surface_allow_list",
        pairs=pairs,
        operations=tuple(sorted(set(operations))),
        source="nexus_ai_agent.bot.creative_surface.CreativeSurfaceMapper.ALLOWED",
        unresolved=tuple(unresolved),
    )


def probe_worker_closed_set() -> SurfaceProbe:
    """Probe 2: what the ``creative_render`` worker will dispatch."""
    from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL

    pairs = tuple(sorted(SURFACE_TO_CANONICAL))
    return SurfaceProbe(
        name="worker_closed_set",
        pairs=pairs,
        operations=tuple(sorted(set(SURFACE_TO_CANONICAL.values()))),
        source="nexus_ai_agent.creative.render_jobs.SURFACE_TO_CANONICAL",
    )


def probe_slideshow_entrypoint(root: Path | None = None) -> SurfaceProbe:
    """Probe 3: the canonical operations the ``/slideshow`` handler dispatches.

    Derived by parsing the ``bus.dispatch(_command(OPERATION_X, …))`` call sites
    of the slideshow service — the module the ``slideshow_render`` job handler
    calls.  A refactor that stops dispatching an operation therefore *shrinks*
    this set and turns the gate red instead of silently keeping a stale claim.
    """
    base = root if root is not None else REPO_ROOT
    path = base / SLIDESHOW_SERVICE
    if not path.is_file():
        raise SourceError(f"slideshow service missing: {path}")

    from nexus_ai_agent.creative.packs.slideshow import operations as slideshow_operations

    operations: list[str] = []
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "_command"):
            continue
        first = node.args[0]
        if not isinstance(first, ast.Name):
            continue
        resolved = getattr(slideshow_operations, first.id, None)
        if isinstance(resolved, str):
            operations.append(resolved)

    if not operations:
        raise SourceError(
            f"{SLIDESHOW_SERVICE} dispatches no canonical operation — the /slideshow "
            "surface would silently stop proving anything"
        )
    return SurfaceProbe(
        name="slideshow_entrypoint",
        pairs=(),
        operations=tuple(sorted(set(operations))),
        source=f"{SLIDESHOW_SERVICE} bus.dispatch(_command(OPERATION_*, …))",
    )


def read_executable_surface(root: Path | None = None) -> Surface:
    """Read the live executable surface through three independent probes."""
    allow_list = probe_surface_allow_list()
    worker = probe_worker_closed_set()

    commands = tuple(sorted(set(probe_registered_commands())))
    return Surface(
        allow_list=allow_list,
        worker_closed_set=worker,
        slideshow_entrypoint=probe_slideshow_entrypoint(root),
        commands=commands,
    )


class _NullQueue:
    """A ``JobQueuePort`` that refuses to be used.

    The surface is inspected by *building* its handler map, and building must
    never enqueue anything.  Every method raises, so a future refactor that
    starts doing work at construction time fails loudly here instead of quietly
    staging a render during a measurement.
    """

    async def enqueue(self, **_kwargs: object) -> str:  # pragma: no cover - never called
        raise AssertionError("the surface must not enqueue while being inspected")

    async def get_status(self, _job_id: str) -> JobStatus:  # pragma: no cover - never called
        raise AssertionError("the surface must not poll while being inspected")

    async def get_result(
        self, _job_id: str
    ) -> dict[str, object] | None:  # pragma: no cover - never called
        raise AssertionError("the surface must not poll while being inspected")


def probe_registered_commands() -> tuple[str, ...]:
    """The command names the surface actually registers with Telegram."""
    from nexus_ai_agent.bot.creative_surface import build_creative_handlers

    return tuple(sorted(build_creative_handlers(_NullQueue()).keys()))


@dataclass(frozen=True)
class RenderLane:
    """The canonical operations the ``creative_render`` worker lane can execute.

    Derived by parsing every ``canonical_id == "<operation>"`` branch of the
    lane module.  A branch is a materialisation path (a render, an OTIO export,
    a caption document) — this is the *executor-ready* measurement, and it is
    deliberately independent of both the registry and the surface map.
    """

    operations: tuple[str, ...]
    source: str


def read_render_lane(root: Path | None = None) -> RenderLane:
    """Derive which canonical operations the render lane has an execution branch for."""
    base = root if root is not None else REPO_ROOT
    path = base / RENDER_JOBS
    if not path.is_file():
        raise SourceError(f"render lane module missing: {path}")

    operations: set[str] = set()
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id == "canonical_id"):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                operations.add(comparator.value)

    if not operations:
        raise SourceError(
            f"{RENDER_JOBS} has no canonical execution branch — the render lane would "
            "silently claim nothing is executable"
        )
    return RenderLane(
        operations=tuple(sorted(operations)),
        source=f"{RENDER_JOBS} `canonical_id == …` execution branches",
    )


# --------------------------------------------------------------------------- #
# Consumed evidence — Gate 4 (never re-derived here)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Gate4Cell:
    """One Gate-4 cell: a slice operation at one cross-layer position."""

    t_id: str
    operation_id: str
    layer: str
    verdict: str


@dataclass(frozen=True)
class Gate4:
    """Gate 4's cross-layer evidence matrix, consumed as-is."""

    cells: tuple[Gate4Cell, ...]
    schema: str
    slice_ids: tuple[str, ...]

    def proven(self, layer: str) -> frozenset[str]:
        """Operation ids whose ``layer`` cell is a PASS."""
        return frozenset(
            cell.operation_id
            for cell in self.cells
            if cell.layer == layer and cell.verdict == "PASS"
        )

    def verdicts(self, layer: str) -> dict[str, str]:
        return {cell.operation_id: cell.verdict for cell in self.cells if cell.layer == layer}


def read_gate4_evidence(root: Path | None = None) -> Gate4:
    """Read Gate 4's matrix.  Gate 2.2 consumes it; it never re-runs the slice."""
    import json

    base = root if root is not None else REPO_ROOT
    path = base / GATE4_EVIDENCE
    if not path.is_file():
        raise SourceError(
            f"Gate 4 evidence missing: {path}.  Gate 2.2 consumes Gate 4's cross-layer "
            "matrix; without it the runtime/artifact layers cannot be claimed."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    slice_map: dict[str, str] = {str(k): str(v) for k, v in (payload.get("slice") or {}).items()}
    cells: list[Gate4Cell] = []
    for layer, verdicts in sorted((payload.get("matrix") or {}).items()):
        for t_id, verdict in sorted(verdicts.items()):
            operation_id = slice_map.get(t_id)
            if operation_id is None:
                continue
            cells.append(
                Gate4Cell(
                    t_id=t_id,
                    operation_id=operation_id,
                    layer=str(layer),
                    verdict=str(verdict),
                )
            )
    if not cells:
        raise SourceError(f"Gate 4 evidence has no matrix cells: {path}")
    return Gate4(
        cells=tuple(cells),
        schema=str(payload.get("schema", "")),
        slice_ids=tuple(sorted(slice_map)),
    )


__all__ = [
    "CATALOG_PATH",
    "GATE4_EVIDENCE",
    "RENDER_JOBS",
    "REPO_ROOT",
    "SLIDESHOW_SERVICE",
    "Catalog",
    "CatalogRow",
    "Gate4",
    "Gate4Cell",
    "RenderLane",
    "Runtime",
    "RuntimeOperation",
    "SourceError",
    "Surface",
    "SurfaceProbe",
    "WAVE1_REGISTRY_SOURCE",
    "probe_registered_commands",
    "probe_slideshow_entrypoint",
    "probe_surface_allow_list",
    "probe_worker_closed_set",
    "read_executable_surface",
    "read_gate4_evidence",
    "read_product_catalog",
    "read_render_lane",
    "read_runtime_registry",
]
