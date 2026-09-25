"""The independent sources of Nagar operation truth, read separately.

Nothing in this module reads a projection or a generated document.  Every
function goes to a *source*: the product catalogue, the live registry object,
the live surface code, the render lane, the declared-operation symbols, the
test-suite execution evidence, or the recorded proof registry.  That is what
makes the recomputation non-self-referential — and what makes the mutation
probes in ``tests/unit/test_operation_truth_mutations.py`` able to turn it red.

The five sources (each an independent predicate over the tree)
----------------------------------------------------------------
``product_catalog``
    ``docs/NAGAR_70_OPERATIONS_TDD.md`` — the rows of the *seven pack
    tables*, in document order (``T01..Tnn``).  Strictly table-scoped: a
    backticked ``a.b`` token in prose (a package id, an example) is not a
    catalogue row.

``runtime_registry``
    ``build_runtime_registry()`` — the composed registry's own answer to
    "which operations can this process dispatch".  Its size is a
    *measurement*, never a constant.

``executable_surface``
    Three mechanically independent probes (surface allow-list, worker closed
    set, slideshow entrypoint) plus the registered command names.
    Disagreement between them is contract drift, recorded not rounded.

``declared_operations``
    The ``operation_id=`` literals of the studio core and the ``OPERATION_*``
    module constants of every pack — the source-level declaration set.
    ``declared == registered`` is the bidirectional guard against fake
    registration and against implementation that never reached the registry.

``proof evidence``
    (a) *suite execution*: recomputed by AST over ``tests/`` — an operation id
    (string literal or imported ``OPERATION_*`` constant) appearing in a test
    module that actually *calls* ``.dispatch``/``._dispatch``/``.handler`` or a
    slideshow service entrypoint.  (b) *recorded proofs*: the append-only
    ``docs/audits/OPERATION_PROOF_REGISTRY.json``, consumed when present,
    schema-checked, never re-derived.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry

#: Repository root, derived from this file (``src/nexus_ai_agent/nagar``).
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Source 1 — the product catalogue.
CATALOG_PATH = Path("docs") / "NAGAR_70_OPERATIONS_TDD.md"

#: A pack table is introduced by this exact header cell (starts-with).
_TABLE_HEADER = "| Operation"

#: A catalogue row is ``| `operation.id` | …`` (backticked id, first cell).
_OPERATION_ROW = re.compile(r"\|\s*`([a-z][a-z0-9_]*\.[a-z0-9_]+)`")

#: An operation id token used to classify free string constants.
_OPERATION_TOKEN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")

#: Where the worker-side closed set lives.
RENDER_JOBS = Path("src") / "nexus_ai_agent" / "creative" / "render_jobs.py"

#: Where the ``/slideshow`` service dispatch sites live.
SLIDESHOW_SERVICE = Path("src") / "nexus_ai_agent" / "creative" / "slideshow" / "service.py"

#: Where the Telegram command handlers are registered.
BOT_HANDLERS = Path("src") / "nexus_ai_agent" / "bot" / "handlers.py"

#: Studio core registry source (operations with no pack manifest).
WAVE1_REGISTRY_SOURCE = "nexus_ai_agent.creative.studio.capabilities.build_wave1_registry()"

#: Recorded proof registry (append-only evidence, consumed never derived).
PROOF_REGISTRY_PATH = Path("docs") / "audits" / "OPERATION_PROOF_REGISTRY.json"

#: Optional cross-gate evidence (consumed as-is when present; absent on main).
GATE4_EVIDENCE = Path("docs") / "audits" / "GATE4_TRUTH_MATRIX.json"

#: Pack operation modules scanned for ``OPERATION_*`` declarations.
_PACK_MODULES = (
    "nexus_ai_agent.creative.packs.slideshow.operations",
    "nexus_ai_agent.creative.packs.caption.operations",
    "nexus_ai_agent.creative.packs.edit.operations",
    "nexus_ai_agent.creative.packs.motion.operations",
    "nexus_ai_agent.creative.packs.audio.operations",
    "nexus_ai_agent.creative.packs.delivery.operations",
)

#: The studio core module scanned for ``operation_id=`` literals.
_STUDIO_MODULE = "nexus_ai_agent.creative.studio.capabilities"

#: Test-directory entrypoints that count as execution of an operation id.
_SERVICE_ENTRYPOINTS = frozenset(
    {"plan_from_files", "render_from_files", "plan_from_session", "upscale_from_file"}
)

#: Attribute names on a call that mean "this test executed something".
_EXECUTION_ATTRS = frozenset({"dispatch", "_dispatch", "handler"})

#: Test files the suite-evidence probe ignores: the truth engine's own tests
#: *observe* the engine (they cite operation ids while mutating projections),
#: they do not evidence those operations.  Excluding them keeps the proof
#: source independent of this package's test suite.
_EVIDENCE_EXCLUDED_STEMS = ("test_operation_truth",)


class SourceError(RuntimeError):
    """A source could not be read, or did not have the shape truth requires."""


# --------------------------------------------------------------------------- #
# Source 1 — Product catalogue
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CatalogRow:
    """One catalogue row: id, table position, and the contract cells."""

    ordinal: int
    operation_id: str
    category: str
    line_number: int
    io_spec: str
    engine: str
    permission_level: str
    pack_id: str

    @property
    def t_id(self) -> str:
        """``T01``-style id, zero-padded, in document order."""
        return f"T{self.ordinal:02d}"

    @property
    def model_dependent(self) -> bool:
        """True when the catalogue's engine cell names a model/ML pipeline.

        A *measurement over the engine cell*, not an opinion: the tokens are
        matched case-insensitively against the cell the catalogue itself
        writes (``ONNX/WebGPU``, ``segmentation + …``, ``inpaint local``,
        ``tracker/Kalman``, ``matting ONNX``, ``face warp model``, …).
        """
        engine = self.engine.lower()
        tokens = (
            "onnx",
            "segmentation",
            "inpaint",
            "tracker",
            "kalman",
            "matting",
            "warp model",
            "model",
            "pyannote",
            "whisper",
            "aligner",
        )
        return any(token in engine for token in tokens)

    @property
    def artifact_kind(self) -> str:
        """Derived from the output half of the Input → Output cell.

        ``materialized_file`` when the catalogue promises a derived asset or a
        media/document file; ``state_revision`` for patches, plans, maps and
        other pure state outputs.
        """
        output = self.io_spec.split("→", 1)[-1] if "→" in self.io_spec else self.io_spec
        materializing = (
            "Derived" in output
            or ".srt" in output
            or ".ass" in output
            or "Asset" in output
            or "video" in output.lower()
        )
        return "materialized_file" if materializing else "state_revision"


@dataclass(frozen=True)
class Catalog:
    """The product catalogue as measured, plus the tables it was parsed from."""

    rows: tuple[CatalogRow, ...]
    table_lines: tuple[int, ...]
    pack_ids: tuple[str, ...]

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(row.operation_id for row in self.rows)

    @property
    def by_id(self) -> dict[str, CatalogRow]:
        return {row.operation_id: row for row in self.rows}

    @property
    def count(self) -> int:
        return len(self.rows)


def _catalog_lines(root: Path) -> list[str]:
    path = root / CATALOG_PATH
    if not path.is_file():
        raise SourceError(f"product catalogue missing: {path}")
    return path.read_text(encoding="utf-8").splitlines()


def _pack_id_for(lines: list[str], table_index: int) -> str:
    """The ``nexus.…`` pack id announced by the nearest section header above
    the table — measured, never guessed from the category name."""
    header_line = table_index
    pack_pattern = re.compile(r"`(nexus\.[a-z0-9_.]+)`")
    while header_line >= 0:
        match = pack_pattern.search(lines[header_line])
        if match and lines[header_line].lstrip().startswith("#"):
            return match.group(1)
        header_line -= 1
    return "UNAVAILABLE"


def read_product_catalog(root: Path | None = None) -> Catalog:
    """Parse the pack tables of the TDD into an ordered catalogue.

    Strictness is the point: rows are only counted *inside* a table whose
    header starts with ``| Operation``; a malformed row (fewer than four
    cells) or a duplicate id raises :class:`SourceError` instead of being
    silently deduplicated, so a duplicated operation id can never slide
    through a recount.
    """
    base = root if root is not None else REPO_ROOT
    lines = _catalog_lines(base)

    rows: list[CatalogRow] = []
    table_lines: list[int] = []
    seen: dict[str, int] = {}
    inside = False
    current_pack = "UNAVAILABLE"

    for line_number, line in enumerate(lines, start=1):
        if line.startswith(_TABLE_HEADER):
            table_lines.append(line_number)
            current_pack = _pack_id_for(lines, line_number - 1)
            inside = True
            continue
        if not inside:
            continue
        if not line.startswith("|"):
            inside = False
            continue
        match = _OPERATION_ROW.match(line)
        if match is None:
            continue
        operation_id = match.group(1)
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            raise SourceError(
                f"{CATALOG_PATH}:{line_number}: catalogue row for {operation_id!r} has "
                f"{len(cells)} cells, expected 4 (id, input→output, engine, level)"
            )
        if operation_id in seen:
            raise SourceError(
                f"{CATALOG_PATH}:{line_number}: duplicate operation id {operation_id!r} "
                f"(first declared on line {seen[operation_id]})"
            )
        seen[operation_id] = line_number
        rows.append(
            CatalogRow(
                ordinal=len(rows) + 1,
                operation_id=operation_id,
                category=operation_id.split(".", 1)[0],
                line_number=line_number,
                io_spec=cells[1],
                engine=cells[2],
                permission_level=cells[3],
                pack_id=current_pack,
            )
        )

    if not rows:
        raise SourceError(f"no pack tables found in {CATALOG_PATH}")
    return Catalog(
        rows=tuple(rows),
        table_lines=tuple(table_lines),
        pack_ids=tuple(dict.fromkeys(row.pack_id for row in rows)),
    )


# --------------------------------------------------------------------------- #
# Source 2 — Live runtime registry
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

        Three mechanical facts: a typed input model that forbids unknown
        fields, a *named* handler (a lambda would hide a placeholder), and
        ``deterministic=True`` declared on the spec.
        """
        return self.input_forbids_extra and self.handler_is_named and self.deterministic

    @property
    def registrar(self) -> str:
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
    def count(self) -> int:
        return len(self.operations)

    @property
    def composition_issues(self) -> tuple[str, ...]:
        from nexus_ai_agent.creative.packs.runtime import composition_issues

        return composition_issues()


def read_runtime_registry(registry: CapabilityRegistry | None = None) -> Runtime:
    """Read the composed registry — the runtime's own answer."""
    if registry is None:
        from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

        registry = build_runtime_registry()
    operations: list[RuntimeOperation] = []
    for operation_id in sorted(registry.list_operations()):
        spec = registry.get_spec(operation_id)
        handler = spec.handler
        handler_name = getattr(handler, "__name__", "") or type(handler).__name__
        named = bool(handler_name) and handler_name not in {"<lambda>", "<unknown>"}
        model_config = getattr(spec.input_model, "model_config", None) or {}
        operations.append(
            RuntimeOperation(
                operation_id=operation_id,
                permission_level=str(
                    getattr(spec.permission_level, "value", spec.permission_level)
                ),
                input_model=f"{spec.input_model.__module__}.{spec.input_model.__qualname__}",
                input_forbids_extra=model_config.get("extra") == "forbid",
                handler_name=handler_name,
                handler_is_named=callable(handler) and named,
                deterministic=bool(getattr(spec, "deterministic", False)),
                required_packs=tuple(getattr(spec, "required_packs", ()) or ()),
            )
        )
    return Runtime(operations=tuple(operations))


# --------------------------------------------------------------------------- #
# Source 3 — Live executable surface
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SurfaceProbe:
    """One independent probe of the executable surface."""

    name: str
    pairs: tuple[tuple[str, str], ...]
    operations: tuple[str, ...]
    source: str
    unresolved: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Surface:
    """The executable surface, as measured by three independent probes."""

    allow_list: SurfaceProbe
    worker_closed_set: SurfaceProbe
    slideshow_entrypoint: SurfaceProbe
    commands: tuple[str, ...]
    slideshow_command_registered: bool

    @property
    def pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset(self.allow_list.pairs)

    @property
    def operation_ids(self) -> tuple[str, ...]:
        """Every canonical operation reachable from a live user entrypoint."""
        return tuple(
            sorted(set(self.allow_list.operations) | set(self.slideshow_entrypoint.operations))
        )

    @property
    def mapped_commands(self) -> frozenset[str]:
        """Commands whose maps reach operations (worker map + slideshow)."""
        commands = {command for command, _ in self.allow_list.pairs}
        if self.slideshow_entrypoint.operations:
            commands.add("slideshow")
        return frozenset(commands)

    @property
    def disagreements(self) -> tuple[str, ...]:
        findings: list[str] = []
        worker_pairs = frozenset(self.worker_closed_set.pairs)
        if self.pairs != worker_pairs:
            findings.append(
                "surface allow-list and worker closed set disagree: "
                f"surface-only={sorted(self.pairs - worker_pairs)} "
                f"worker-only={sorted(worker_pairs - self.pairs)}"
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
            findings.append("the surface registers no creative command")
        return tuple(findings)


def _tree(path: Path) -> ast.AST:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:  # pragma: no cover - defensive
        raise SourceError(f"cannot parse {path}: {exc}") from exc


def probe_surface_allow_list() -> SurfaceProbe:
    """Probe 1: what the surface's own mapper accepts, resolved through the
    worker map (an unresolvable pair is recorded, never dropped)."""
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
    """Probe 2: what the ``creative_render`` worker will dispatch, or die."""
    from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL

    pairs = tuple(sorted(SURFACE_TO_CANONICAL))
    return SurfaceProbe(
        name="worker_closed_set",
        pairs=pairs,
        operations=tuple(sorted(set(SURFACE_TO_CANONICAL.values()))),
        source="nexus_ai_agent.creative.render_jobs.SURFACE_TO_CANONICAL",
    )


def probe_slideshow_entrypoint(root: Path | None = None) -> SurfaceProbe:
    """Probe 3: canonical operations reached from the ``/slideshow`` service.

    Derived by parsing the ``bus.dispatch(_command(OPERATION_X, …))`` call
    sites; a refactor that stops dispatching an operation *shrinks* this set
    and turns the gate red instead of silently keeping a stale claim.
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


class _NullQueue:
    """A ``JobQueuePort`` that refuses to be used: building the handler map
    must never enqueue anything."""

    async def enqueue(self, **_kwargs: object) -> str:  # pragma: no cover - never called
        raise AssertionError("the surface must not enqueue while being inspected")

    async def get_status(self, _job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("the surface must not poll while being inspected")

    async def get_result(self, _job_id: str) -> dict[str, object] | None:  # pragma: no cover
        raise AssertionError("the surface must not poll while being inspected")


def probe_registered_commands(root: Path | None = None) -> tuple[str, ...]:
    """The creative commands the surface registers with Telegram, plus the
    ``/slideshow`` command handler when ``bot/handlers.py`` declares it."""
    from typing import cast

    from nexus_ai_agent.application.ports.job_queue import JobQueuePort
    from nexus_ai_agent.bot.creative_surface import build_creative_handlers

    base = root if root is not None else REPO_ROOT
    commands = set(build_creative_handlers(cast(JobQueuePort, _NullQueue())).keys())
    if probe_slideshow_command_registered(root=base):
        commands.add("slideshow")
    else:
        commands.discard("slideshow")
    return tuple(sorted(commands))


def probe_slideshow_command_registered(root: Path | None = None) -> bool:
    """True when ``bot/handlers.py`` registers ``CommandHandler("slideshow", …)``."""
    base = root if root is not None else REPO_ROOT
    path = base / BOT_HANDLERS
    if not path.is_file():
        return False
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "CommandHandler"):
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "slideshow"
        ):
            return True
    return False


def read_executable_surface(root: Path | None = None) -> Surface:
    """Read the live executable surface through the independent probes."""
    base = root if root is not None else REPO_ROOT
    allow_list = probe_surface_allow_list()
    worker = probe_worker_closed_set()
    commands = tuple(sorted(set(probe_registered_commands(base))))
    return Surface(
        allow_list=allow_list,
        worker_closed_set=worker,
        slideshow_entrypoint=probe_slideshow_entrypoint(base),
        commands=commands,
        slideshow_command_registered=probe_slideshow_command_registered(root=base),
    )


# --------------------------------------------------------------------------- #
# Source 4 — Render lane (production execution branches)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RenderLane:
    """Canonical operations the ``creative_render`` lane has a branch for."""

    operations: tuple[str, ...]
    source: str


def read_render_lane(root: Path | None = None) -> RenderLane:
    """Parse every ``canonical_id == "<operation>"`` branch of the lane."""
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
# Source 5 — Declared operation symbols (studio literals + pack constants)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Declared:
    """The source-level declaration set, measured per origin."""

    studio_literals: tuple[str, ...]
    pack_constants: tuple[str, ...]

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.studio_literals) | set(self.pack_constants)))

    @property
    def count(self) -> int:
        return len(self.operation_ids)


def read_declared_operations() -> Declared:
    """Resolve the declaration symbols of the studio core and every pack.

    * studio core: string literals passed as ``operation_id=…`` (the wave-1
      registrations);
    * packs: module constants named ``OPERATION_*`` (every pack declares its
      ids that way — resolved by *import*, so concatenations like
      ``DOMAIN + ".x"`` are followed, not pattern-guessed).
    """
    module = importlib.import_module(_STUDIO_MODULE)
    if not module.__file__:  # pragma: no cover - defensive
        raise SourceError(f"{_STUDIO_MODULE} has no source file to scan")
    module_path = Path(module.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    studio_literals = sorted(
        {
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg == "operation_id"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and _OPERATION_TOKEN.match(node.value.value)
        }
    )

    pack_constants: set[str] = set()
    for module_name in _PACK_MODULES:
        pack_module = importlib.import_module(module_name)
        for name, value in vars(pack_module).items():
            if name.startswith("OPERATION_") and isinstance(value, str):
                if not _OPERATION_TOKEN.match(value):
                    raise SourceError(f"{module_name}.{name} = {value!r} is not an operation id")
                pack_constants.add(value)

    return Declared(
        studio_literals=tuple(studio_literals), pack_constants=tuple(sorted(pack_constants))
    )


# --------------------------------------------------------------------------- #
# Source 6 — Proof evidence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SuiteEvidence:
    """Suite-execution evidence for one operation, as measured by AST."""

    operation_id: str
    kinds: tuple[str, ...]
    files: tuple[str, ...]

    @property
    def executed(self) -> bool:
        """At least one test module *calls* an execution entrypoint with this id."""
        return bool(self.kinds)


@dataclass(frozen=True)
class RecordedProof:
    """One entry of the append-only recorded proof registry."""

    operation_id: str
    kind: str
    method: str
    recorded_at: str
    source_revision: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class Proof:
    """Both halves of the proof source: recomputed suite execution, plus the
    recorded registry (consumed, never re-derived)."""

    suite: dict[str, SuiteEvidence]
    recorded: tuple[RecordedProof, ...]
    registry_schema: str
    registry_present: bool

    def suite_kinds(self, operation_id: str) -> tuple[str, ...]:
        entry = self.suite.get(operation_id)
        return entry.kinds if entry else ()

    def recorded_for(self, operation_id: str, kind: str) -> tuple[RecordedProof, ...]:
        return tuple(p for p in self.recorded if p.operation_id == operation_id and p.kind == kind)

    @property
    def recorded_ids(self) -> frozenset[str]:
        return frozenset(p.operation_id for p in self.recorded)


def _execution_kinds(tree: ast.AST) -> set[str]:
    """Which execution idioms a test module uses (call shape, not prose)."""
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr in _EXECUTION_ATTRS:
                    kinds.add(func.attr.lstrip("_"))
                elif func.attr in _SERVICE_ENTRYPOINTS:
                    kinds.add("service")
            elif isinstance(func, ast.Name):
                if func.id in _SERVICE_ENTRYPOINTS:
                    kinds.add("service")
        elif isinstance(node, ast.ImportFrom):
            if node.module and (
                "slideshow.service" in node.module or node.module.endswith("slideshow.service")
            ):
                kinds.add("service")
    return kinds


def read_suite_evidence(
    operation_ids: set[str], root: Path | None = None
) -> dict[str, SuiteEvidence]:
    """Scan ``tests/`` for operations the suite *executes*.

    An operation counts as executed only when its id — as a string literal or
    as an imported ``OPERATION_*`` constant resolved through the module —
    appears in a test module that also *calls* an execution entrypoint
    (``.dispatch``/``._dispatch``/``.handler`` or a slideshow service
    function).  Referenced-but-never-called files are recorded as references
    only (empty ``kinds``), which is exactly the PARTIAL proof tier.
    """
    base = root if root is not None else REPO_ROOT
    tests_root = base / "tests"
    if not tests_root.is_dir():
        raise SourceError(f"tests directory missing: {tests_root}")

    # id -> kind -> files, and id -> every file that names it
    found: dict[str, dict[str, set[str]]] = {}
    referenced: dict[str, set[str]] = {}

    for path in sorted(tests_root.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        if path.stem.startswith(_EVIDENCE_EXCLUDED_STEMS):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - defensive
            raise SourceError(f"cannot parse {rel}: {exc}") from exc

        # Collect candidate ids: literals + imported OPERATION_* constants.
        candidates: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in operation_ids:
                    candidates.add(node.value)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if ".creative.packs." in node.module and node.module.endswith(
                    (".operations", "slideshow")
                ):
                    try:
                        module = importlib.import_module(node.module)
                    except Exception:  # pragma: no cover - defensive
                        continue
                    for alias in node.names:
                        value = getattr(module, alias.name, None)
                        if isinstance(value, str) and value in operation_ids:
                            candidates.add(value)

        if not candidates:
            continue
        kinds = _execution_kinds(tree)
        for operation_id in candidates:
            referenced.setdefault(operation_id, set()).add(rel)
            if kinds:
                found.setdefault(operation_id, {})
                for kind in sorted(kinds):
                    found[operation_id].setdefault(kind, set()).add(rel)

    evidence: dict[str, SuiteEvidence] = {}
    for operation_id in sorted(operation_ids):
        op_kinds = tuple(sorted(found.get(operation_id, {})))
        files = tuple(sorted(referenced.get(operation_id, set())))
        evidence[operation_id] = SuiteEvidence(
            operation_id=operation_id, kinds=op_kinds, files=files
        )
    return evidence


def read_recorded_proofs(root: Path | None = None) -> tuple[str, tuple[RecordedProof, ...]]:
    """Read the append-only proof registry; absent file ⇒ empty, schema recorded."""
    base = root if root is not None else REPO_ROOT
    path = base / PROOF_REGISTRY_PATH
    if not path.is_file():
        return "ABSENT", ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceError(f"proof registry is not valid JSON: {path}: {exc}") from exc
    schema = str(payload.get("schema", ""))
    if not schema.startswith("nagar.operation_proof_registry."):
        raise SourceError(f"proof registry has an unknown schema {schema!r}: {path}")
    proofs: list[RecordedProof] = []
    for entry in payload.get("proofs", []):
        if not isinstance(entry, dict):
            raise SourceError(f"proof registry entry is not an object: {path}")
        missing = [
            key for key in ("operation_id", "kind", "method", "recorded_at") if key not in entry
        ]
        if missing:
            raise SourceError(f"proof registry entry lacks {missing}: {path}")
        proofs.append(
            RecordedProof(
                operation_id=str(entry["operation_id"]),
                kind=str(entry["kind"]),
                method=str(entry["method"]),
                recorded_at=str(entry["recorded_at"]),
                source_revision=str(entry.get("source_revision", "UNKNOWN")),
                evidence=dict(entry.get("evidence", {})),
            )
        )
    return schema, tuple(proofs)


def read_proof(root: Path | None = None, operation_ids: set[str] | None = None) -> Proof:
    """Read both proof halves.  ``operation_ids`` defaults to the live universe."""
    if operation_ids is None:
        operation_ids = set(read_product_catalog(root).operation_ids) | set(
            read_runtime_registry().operation_ids
        )
    suite = read_suite_evidence(operation_ids, root=root)
    schema, recorded = read_recorded_proofs(root=root)
    return Proof(
        suite=suite, recorded=recorded, registry_schema=schema, registry_present=schema != "ABSENT"
    )


def read_gate4_evidence(root: Path | None = None) -> dict[str, Any] | None:
    """Consume Gate 4's cross-gate matrix when present (never re-derived)."""
    base = root if root is not None else REPO_ROOT
    path = base / GATE4_EVIDENCE
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceError(f"gate4 evidence is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "matrix" not in payload:
        raise SourceError("gate4 evidence has no matrix")
    return payload


__all__ = [
    "BOT_HANDLERS",
    "CATALOG_PATH",
    "GATE4_EVIDENCE",
    "PROOF_REGISTRY_PATH",
    "RENDER_JOBS",
    "REPO_ROOT",
    "SLIDESHOW_SERVICE",
    "WAVE1_REGISTRY_SOURCE",
    "Catalog",
    "CatalogRow",
    "Declared",
    "Proof",
    "RecordedProof",
    "RenderLane",
    "Runtime",
    "RuntimeOperation",
    "SourceError",
    "SuiteEvidence",
    "Surface",
    "SurfaceProbe",
    "probe_registered_commands",
    "probe_slideshow_command_registered",
    "probe_slideshow_entrypoint",
    "probe_surface_allow_list",
    "probe_worker_closed_set",
    "read_declared_operations",
    "read_executable_surface",
    "read_gate4_evidence",
    "read_product_catalog",
    "read_proof",
    "read_recorded_proofs",
    "read_render_lane",
    "read_runtime_registry",
    "read_suite_evidence",
]
